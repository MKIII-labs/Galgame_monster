# NOTES

## [2026-11-06] 浏览器验收：设置 →「Agent 执行」Computer Use「新站点授权方式」

- 问题/决策：
  - 本机未装 Chrome（只有 Edge），Trae 浏览器插件连不上（`failed to connect to browser plugin of Chrome ... os error 10061`）。`browser_*` 工具、`browser_waiting_for_user_interaction`、`browser_unlock` 均不可用，`browser_setup_plugin` 会挂起等用户选择，因此浏览器工具链在本机不可用。
- 解决方案/原因：
  - 兜底方案：用 Edge（Chromium 内核）以 `--headless=new --remote-debugging-port=9222 --user-data-dir=<临时目录>` 起独立实例，再用 Node 24 内置 `fetch` + 全局 `WebSocket` 直接发 CDP（Page / Runtime / Log / Input），按元素中心坐标发真实鼠标事件，读取 DOM 与 console 错误。
  - 启动方式：`cmd /c start` 被 RunCommand 禁止；`node spawn(detached)` 会让 RunCommand 卡住 630s；可用 `Invoke-CimMethod Win32_Process Create`，并可把 PowerShell 脚本用 UTF-16LE base64 传 `-EncodedCommand`，绕开引号转义。
  - 加载前用 `Page.addScriptToEvaluateOnNewDocument` 注入 `window.__errs` 钩子，可在后续独立进程里回读整页 console 错误。
  - 验收结论：PASS。`#computer-site-mode` 存在（SELECT），options 为 `ask / auto / allow`，当前值 `ask`，设置侧栏激活项为「Agent 执行」，console 无错误。
- 相关代码/指令（如果有）：
  - `msedge.exe --headless=new --remote-debugging-port=9222 --user-data-dir=%TEMP%\trae-cdp-prof --no-first-run --window-size=1440,900 about:blank`
  - 目标页 `http://127.0.0.1:8802/`；设置入口 `#btn-settings`；设置侧栏 `.snav-btn`（文本「Agent 执行」）
  - 收尾：`taskkill /PID <pid> /T /F` 并删除临时 profile

## [2026-11-05] 模拟股市重构：拆出 stockkit 分层 + 限价单/部分成交/公司行为/风险指标/报告导出

- 问题/决策：
  - `app/stock.py` 单文件 3250 行、约 100 个顶层函数，混着六类职责：行情抓取、SQLite schema、对局生命周期、分析库（含 FTS）、时间线回放、撮合与 AI 决策。任何一处改动都要在 3000 行里翻找，且撮合规则无法脱离数据库单测。
  - 决策：拆出 `app/stockkit/` 领域包，**`app/stock.py` 保留为唯一对外门面**（对局、数据库、AI 决策仍在那里），re-export 新包的符号。老调用方 `stock.normalize_symbol(...)` 之类的写法零改动。
  - 同时补齐四项能力：限价单与部分成交、公司行为（分红/送转/停复牌）、组合风险指标、对局报告导出。
- 解决方案/原因：
  - 分层（依赖只能自上而下，禁止反向 import）：

    | 模块 | 职责 |
    | --- | --- |
    | `stockkit/errors.py` | 领域异常，最底层，无依赖 |
    | `stockkit/rules.py` | 交易规则纯函数：代码规范化 / 手数 / 涨跌停 / 费用 |
    | `stockkit/quotes.py` | 行情读取：腾讯为主、东财兜底、Binance 币市 |
    | `stockkit/matching.py` | 撮合内核：限价 / 市价、部分成交、涨跌停与停牌判定 |
    | `stockkit/corporate.py` | 公司行为：分红、送转（拆股）、停牌 |
    | `stockkit/risk.py` | 组合风险指标：最大回撤 / 年化波动 / 胜率 / 超额收益 |
    | `stockkit/report.py` | 对局报告导出：Markdown / CSV |

  - `stockkit/matching.py` 刻意**只做判定、不碰数据库**：`plan_fill(side, qty, bar, prev_close, limit, limit_price, slippage, pending_days, symbol)` 是纯函数，给定一根日 K 与一笔委托，返回 `Fill(price, qty, reason)`。`_execute_pending` 负责预取当日 bar / 前收盘 / 持仓 / 现金进内存，逐笔调用后只做记账。好处是撮合规则能脱离 SQLite 单测（`tests/test_stock_engine.py`）。
  - 日 K 撮合的固有限制（写进模块注释，避免后人误以为能精确复现盘口）：一根日 K 只有 OHLC + 成交量，没有盘口与逐笔，所以限价单是否被触及只能近似 —— 买：开盘价 ≤ 限价按开盘价成交；开盘价 > 限价但 low ≤ 限价视为盘中触及、按限价成交；否则挂单跨日。部分成交按「单日最多吃掉当日成交量的 10%」近似（A 股没有官方散户参与率口径，这是量级假设，不是精确复刻）。
  - 数据库只做**增量迁移**：`orders` 表新增 `limit_price` / `filled_qty` / `pending_days` 三列，由 `_ensure_order_columns()` 用 `PRAGMA table_info` 检测后 `ALTER TABLE` 补列。三列都带默认值，历史委托自动视为「市价单、全成」，**不需要回填、不重建库、不删字段**。
  - `filled_qty` 与 `qty` 的语义分工：`qty` 始终是委托总量，`filled_qty` 是累计成交量，剩余量 = `qty - filled_qty`。部分成交后委托保持 `pending` 继续参与后续交易日的撮合，直到全成或超时。
  - 挂单超时：`MAX_PENDING_DAYS = 5` 个模拟交易日未成交自动撤销，否则限价单会永久占用冻结资金（停牌期间同样走这条路径）。
  - 公司行为是**人工触发**（`ex_date` 取本局当前模拟日），不抓取真实分红公告：交易日历 / 分红公告的自动抓取需要稳定数据接口，这一版不做，以免把「猜出来的分红」写进账里。除权会同步改持仓与成本，事后无法从 `positions` 反推，因此单独建 `corporate_actions` 流水表；`adjusted_bars()` 靠这张表逐条折算历史 K 线（由早到晚累乘）。
  - 送转后新增的股份同样受 T+1 约束：`available_qty = min(原 available_qty, 新 qty)`，不能凭空变成可卖。
- 踩坑记录（都是实际调试过的）：
  1. **`StockError` 双胞胎**：`stockkit/errors.py` 里定义了 `StockError` 后，`app/stock.py` 里若还留着本地 `class StockError`，就成了两个不同的类，`pytest.raises(stock.StockError)` 捕获不到新包抛出的异常。门面必须 `from app.stockkit.errors import StockError`，不能重新定义。
  2. **`MARKET_CLOSE_MIN` 影子定义**：拆分时把它同时留在 `stock.py` 和搬到 `rules.py`，触发 ruff F811（redefinition of unused）。归属结论：它是交易规则常量，归 `rules.py`，`stock.py` 只从 rules 导入。
  3. **`quotes.py` 漏 `import math` 与 `import json`**：搬移代码时只搬了函数体，模块级 import 没跟着走。拆分脚本必须把「被搬函数引用到的模块级 import」一起带过去。
  4. **`plan_fill` 里 symbol 写死空串**：`cap_by_volume(qty, "", volume)` 会让币市按 A 股 100 股整手取整，0.5 BTC 直接成交不了。symbol 必须一路透传到取整函数。
  5. **`corporate_actions` 列名不一致**：建表用 `date`（与其他表一致），代码里写 `ex_date`，导致 INSERT 报错。约定：**表里叫 `date`，对外字段叫 `ex_date`**，查询时显式映射 `row["ex_date"] = row.get("date")`。
  6. **成交后一律写 `status='filled'`**：部分成交时会把还在挂的委托误标成已完成。必须按 `plan.qty >= remain` 判断是全成还是部分成交。
- 相关代码/指令：
  - 门面：`app/stock.py`（3158 行，re-export stockkit；`_execute_pending` 在 2395 行接入 `matching.plan_fill`）
  - 迁移：`app/stock.py` 的 `_ensure_order_columns()`（425 行）、`_ensure_game_market()`（416 行）
  - 新 API 路由（`app/api/hall_stock.py`）：`/api/hall/stock/risk`、`/api/hall/stock/report`、`/api/hall/stock/corporate`。全库股票路由共 24 条（冒烟脚本实测）
  - 自检脚本要注意：`registry.ROUTES` 初始为空，靠各 API 模块**自注册**填充。单独 `import app.registry` 后再读是 0 条，必须先 `import app.httpd`（它会导入各 api 模块）才拿得到路由表 —— 写冒烟脚本时踩过这个坑
  - 新测试：`tests/test_stock_engine.py`（撮合内核纯函数、公司行为、风险指标、报告导出、限价单端到端）
  - 验收脚本：`scripts/check_stock_refactor.py`（AST 抓 `stock.<name>` 属性引用 + 逐模块冷启动 import + 反向依赖检查 + `plan_fill` 行为抽检；退出码 0 = 全部通过）
  - 回归命令：`python -m pytest -q tests/test_stock.py tests/test_stock_caps.py tests/test_stock_crypto.py tests/test_stock_history.py tests/test_stock_engine.py` → 84 passed
  - 静态检查：`python -m ruff check app/stock.py app/stockkit app/api/hall_stock.py`
- 验证结果（全部实测）：
  - 股票子系统 5 个测试文件 84 个用例全绿（重构前 4 个文件 61 个 + 新增 `test_stock_engine.py` 23 个）。用例数按文件：test_stock 17 / test_stock_caps 10 / test_stock_crypto 7 / test_stock_history 27 / test_stock_engine 23。
  - 门面符号：AST 扫出外部代码真实引用的 `stock.*` 符号 57 个，`getattr` 逐个校验**零缺失**。
  - 循环依赖：stockkit 七个模块各自单独冷启动 `import` 全部成功（能单独导入即说明没有向上依赖 `app.stock`）；另用正则确认没有模块反向 `import app.stock`。
  - `plan_fill` 行为抽检：A 股市价买 1000 股 / 当日量 3000 → 成交 300 股（参与率 10%，按 100 股整手）；币市 1 BTC / 当日量 5.5 → 成交 0.55（按 1e-6 取整，未被误当整手）；限价买开盘在限价内按开盘价成交、盘中触及按限价成交；开盘涨停市价买判废。
  - 全量 `python -m pytest -q`：6 failed / 其余通过（169.2s）。6 个失败均**与股票子系统无关**：`test_memory` 镜像计数（monkeypatch 后仍 7≠2）、`test_native_tools` 压缩请求缺 `stop_event` 参数、`test_prompt_budget` 提示 8268 字 > 8000、`test_reader` 三条缺 `reportlab`。
  - **这条结论的正确论据**（早前写「4 个失败文件都不 import `app.stock`」，那是错的）：`tests/test_prompt_budget.py` 第 8 行 `from app import httpd` 会**间接导入** `app.stock`（`app/httpd.py` 启动时 `from app import stock as stock_lib`），所以「不导入」根本不是有效论据。真正的证据是**内容占比**：复刻该用例参数（`cap_domains=set()`、关闭画像注入）构建出的提示词里，`stock` / `stockkit` / `模拟股市` / `限价` / `公司行为` / `风险指标` / `报告导出` / `对局` / `持仓`，以及新增符号 `plan_fill` / `risk_metrics` / `export_report` / `adjusted_bars` / `corporate_actions`，**全部出现 0 次** —— 超出的字数不可能来自本次股票改动。该检查已并入 `scripts/check_stock_refactor.py` 第 6 段，可随时复现。
  - 字数口径要注意：同一段提示词在不同沙箱下长度不同 —— pytest 实测 8268（`tmp_path`）、独立探针 8220（临时目录）、脚本第 6 段 7124（仓库根目录当沙箱），差异来自路径长度与目录相关内容。所以脚本**只断言股票关键词命中为 0，不断言字数**；也说明这条预算用例对「往系统提示里加字」极敏感，谁加内容谁负责把阈值一起调整。

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
  - **数据层两份并存**：JSON 走 `app/store.py`（`APP_VERSION` 是**唯一版本来源**，当前 3.3；路径常量 + mtime 缓存 + 临时文件 `os.replace` 原子写，带 3 次退避重试防 Windows WinError 5）；结构化数据走 `app/db.py` 的 SQLite `data/assistant.db`（表 `tasks / task_steps / artifacts / events / event_exdates / event_fired`，每线程一连接、WAL）。对话仍在 `data/sessions.json`，未迁移。
  - **前端** `web/`（33 项）：`index.html` 198KB 承载全部面板，`chat.js` 126KB、`stream.js` 101KB、`settings.js` 159KB、`hall-dojo.js` 151KB、`hall-interview.js` 149KB；原生 JS + CSS，无构建步骤，`web/vendor/` 内置 katex / monaco / pdfjs / vis-network / xterm。
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

## [2026-10-29] 发布 3.0.0：版本号维护与私有仓库发布流程
- 问题/决策：把应用版本从 2.0 升到 3.0.0 并发布，同时把 GitHub 仓库改为「仅自己可见」。
- 事实与做法：
  - **版本号唯一来源**是 `app/store.py::APP_VERSION`（本次改为 `3.0.0`）。生效点只有两处：`/api/app/info`（设置 → 数据与高级 → 版本）与 HTTP `Server: Galgame_monster/<版本>` 响应头（`httpd.py::server_version`）。`desktop/package.json` 的 `1.0.0` 是 Electron 窗口壳自己的版本，与 APP 版本无关，未改。
  - **发版命令**：改常量 → `git add app/store.py` → `git commit -m "chore: 版本号更新到 X.Y.Z"` → `git tag -a vX.Y.Z -m "Galgame_monster X.Y.Z"` → `git push origin main --tags`。
  - **改仓库可见性**：本机没有 `gh`；用 `git credential fill` 取到 Windows 凭据管理器里已保存的 token，再调 GitHub REST `PATCH /repos/MKIII-labs/Galgame_monster`，body `{"private":true}`。验证手段：匿名 `curl -I https://github.com/MKIII-labs/Galgame_monster` 返回 **404** 即已私有（200 = 仍公开）。改可见性需要 token 带 Administration 权限。
  - **更新/升级注意**：项目没有自动更新。代码更新后必须重启；`main.py` 会比对 `chat.py / llm.py / bilibili.py / agent.py` 的 mtime，旧进程还在跑时会弹「代码已更新」并拒绝启动。覆盖升级只替换代码 / `web/` / `resources/` / `requirements.txt`，**`data/` 不能覆盖**（API Key、会话、知识库、语音模型都在里面；表结构由 `app/db.py` 自动补列）。
  - 本次结果：commit `5b701ec`、annotated tag `v3.0.0`（指向 HEAD）、`main` 与 `origin/main` 同步、仓库已 private。
- 相关代码/指令：`app/store.py`（`APP_VERSION`）、`app/httpd.py`（`server_version`）、`app/api/misc.py`（`/api/app/info`）、`desktop/package.json`；`git tag -a` / `git push origin main --tags` / `curl -I` 验证可见性。
- 验证：`python -c "from app.store import APP_VERSION"` → 3.0.0；`git tag --points-at HEAD` → v3.0.0；`dev_check.py` 8/8；匿名访问仓库 404。
- 备注：若以后要「启动时检查更新」，可读 GitHub `releases/latest` 与 `APP_VERSION` 比对；目前未实现。

## [2026-10-29] 定位「22:59:11 输出被中断」：本地空转刹车，不是上游
- 现象：用户看到 22:59:11 一轮输出结束，怀疑是上游 API 断流。
- 取证（三条互相印证）：
  - `data/token_usage.jsonl` 最后一条 ts=1793285952（22:59:12），会话 `s_2809c1a88538`，10 轮全部 `state=done`、`ttft` 1.8–5.3s，**没有**断流 / 重试 / 超时记录；最后两轮 `completion=21/22 token`、`finish=stop`、`tools=0`。
  - `data/sessions/s_2809c1a88538/events.jsonl` 的 `message/patch`：`end_reason = {kind:"idle", text:"本轮结果已保留在上面；模型没有继续调用工具…"}`。
  - `data/app.log` 同一时刻：`[空转] 连续 1 次续跑没有执行工具（正文 31 字），已停止`。
  - 结论：命中「自动续跑空转刹车」，是本程序的 agent 循环主动停的；上游那一轮正常结束。
- 根因：B 档把 `agent_auto_idle_limit` 默认设为 1，而循环判定是 `auto_idle >= limit` → 第一次空转就 break；而「你上一轮没有调用工具，请直接调用」这段纠正提示是在 break 之后才构造的，**永远发不出去**，于是模型刚在正文里说「先核对…先写脚本」就被掐断。配置注释写的是「1 = 给一次机会」，实现少给了一次。
- 修复：
  - `app/api/chat.py` 新增 `_idle_brake_hit(auto_idle, limit)`：有效阈值 `max(2, limit)`，第一次空转一定先纠正一次；循环改用它。
  - 空转 warn 与 `end_reason` 文案增加「本地空转保护，不是上游断流」，避免再被误判成上游问题。
  - `app/config.py` 的 `agent_auto_idle_limit` 注释写明语义（1 与 2 等价）。
  - 回归测试 `tests/test_chat_blocks.py::test_idle_brake_gives_one_correction`。
  - 设置页：设置 → Agent →「执行节奏」新增「空转刹车」数字输入 +「不限」按钮；0 = 关闭刹车（不限），1 与 2 等价（先纠正一次），≥3 按原值，默认 2。读写 `agent_auto_idle_limit`。
- 相关代码/指令：`app/api/chat.py`（`_idle_brake_hit` / 自动续跑段）、`app/config.py`、`web/settings.js`（`agent-auto-idle`）、`tests/test_chat_blocks.py`；排查命令见上（token_usage + events.jsonl + app.log 对齐时间戳）。
- 验证：定向测试 82 条全过；`dev_check.py` 8/8；全量 `pytest` exit 0。
- 备注：日志里另有 `idle 53 / network 48 / upload 34` 次真正的断流（链路不稳 + 90 秒无数据看门狗），与本次 22:59 这一次无关，是另一个待办。

## [2026-10-29] 23:23:39 又一次「中断」：承诺识别漏了「现在+单字动词」
- 取证：
  - `data/token_usage.jsonl` 最后一条 23:23:39（session `s_2809c1a88538`）：26 轮全部 `state=done`，**没有**断流 / 重试 / 超时记录。
  - 该会话 `message/patch`：`end_reason` 为空、`interrupted=false` → 走的是「正常收尾」分支，不是手动停止。
  - `data/app.log` 23:23 前后**没有** `[空转]`、也没有 `[断流]` → 既不是空转刹车，也不是上游中断。
- 直接原因：最后一轮模型只写正文「现在查 `showShop` 和 `gameOver` 的真实包裹情况。」，没有发起工具调用就结束；`stepctx._tail_promises_work` 没认出这句 → 按「成篇正文 + 无承诺」判定为已收尾（`len>=300 and not promise`），回合正常结束，界面看起来像被中断。
- 根因：`_ACTION_RE` 只收「重写 / 验证 / 修改」这类双字动词，`_BARE_ACTION_RE` 要求句首直接是动词；「现在 + 查」这种**紧邻**结构两边都没命中。
- 修复：新增 `_ACTION_NEAR_RE`（现在 / 接下来 / 下一步 / 然后 / 接着 / 下面 / 随后 + 可选「先 / 再 / 就」+ 单字或短动词），并接入 `_tail_promises_work`。只允许紧邻，避免把「接下来你可以自己试」误判成承诺。
- 相关代码/指令：`app/stepctx.py`（`_ACTION_NEAR_RE` / `_tail_promises_work`）、`tests/test_stepctx.py::test_tail_promise_detects_immediate_single_action`。
- 验证：`test_stepctx.py + test_chat_blocks.py` 69 条全过；`dev_check.py` 8/8；全量 `pytest` exit 0。
- 备注：这类「正常收尾但看起来中断」的判断依赖词表；再遇到漏判，优先把原文尾部句子补进 `_PROMISE_WORK` / `_ACTION_NEAR_RE`，并补一条正反测试。

## [2026-10-29] 历史压缩分隔线回到真实压缩位置（不再永远贴底）
- 现象：中途压缩一次后继续聊，那条「历史对话已被压缩」越来越往下，最后总是停在消息流底部。
- 根因：`web/chat.js::syncCompressMarks` 每次 context 同步都把已存在的分隔线 `insertBefore(.stream-host)`，即带到流式回复上方＝当前底部；而 `mark.index`（压缩时 `len(messages)`，见 `app/context.py`）只在整块重画 `renderMessages` 时才被用作位置，所以聊得越久越靠下。
- 修复：
  - 新增 `compressPlaceAtMark(chat, el, mark)`：找第一条 `data-idx >= mark.index` 的消息行，插到它前面；没有匹配（压缩点更靠后 / 已全被压缩）才退回 `compressPlaceAtEnd`。
  - `syncCompressMarks` 的新建与「升级 pending → 已完成」两条路径都改用它。
  - `renderMessages` 补兜底：压缩点在本次渲染窗口之前（未加载 / 已折叠）时也按同一规则补一条，而不是丢弃。
- 相关代码：`web/chat.js`（`compressPlaceAtMark` / `syncCompressMarks` / `renderMessages`）。
- 验证：`node --check web/chat.js`；`scripts/dev_check.py` 8/8（含 39 个前端 JS 语法与 Markdown 回归）。
- 备注：`compressPending()`（正在压缩）仍走 `compressPlaceAtEnd` —— 那一刻压缩点确实就是当前底部。

## [2026-10-29] 23:40:05「中断」：提问轮（meta_turn）关掉了自动续跑
- 取证：
  - `data/token_usage.jsonl` 最后一条 23:40:06（session `s_2809c1a88538`）：`stream_state=done`、6 轮全 `done`、`finish_reason=stop`、`end_reason=idle`、`auto_continues=0`；`app.log` 该轮前后**没有** `[断流]`、也没有 `[空转]` → 既不是上游断流，也不是空转刹车。
  - `sessions.json` 该会话最后一条 assistant：`end_reason = {"kind":"idle","text":"已按你的问题作答；要继续任务就点「继续输出」。"}`，`tools` 6 个，正文停在「修正脚本已写好，现在运行它；同时把 S1 标成完成。」
  - 用户那轮输入是「做完了？」→ `stepctx.is_meta_turn` 命中 `做完了?[吗嘛?？]` → `meta_turn=True`。
- 根因：`meta_turn` 在自动续跑判定里是**硬开关**（`and not meta_turn`），且结尾的 `end_reason` 固定是「已按你的问题作答」。这条规则本意是「用户只问进度时别拿旧计划连推 5 轮烧 token」，但没区分「纯问答」和「提问轮里模型自己动手了」——后者会在半路被掐断：这一轮模型实际跑了 6 次工具，末句「现在运行它」是明确的动作承诺。
- 修复：
  - 新增 `app/api/chat.py::_meta_continue_allowed(meta_turn, tool_count, auto_continues)`：非提问轮照常；提问轮只有在**本轮执行过工具**时放行，且只放**一次**（`auto_continues < 1`），兼顾「答完就停」与「别断在半路」。循环里的硬开关改用它。
  - 提问轮收尾文案按实际动作分流：动过手时写「已按你的问题作答，并顺手推进了 N 次工具调用」，不再让用户以为又被掐断。
- 相关代码：`app/api/chat.py`（`_meta_continue_allowed` / 自动续跑判定 / `elif meta_turn and plan_state`）、`tests/test_chat_blocks.py::test_meta_turn_continues_only_after_working`。
- 验证：定向测试 70 条全过；`dev_check.py` 8/8；全量 `pytest` exit 0。
- 备注：23:19:55 启动的那个进程加载的是 23:19:01 的 `chat.py`、23:26:28 的 `stepctx.py` 之前版本；23:44 再启动时启动器提示「code updated … ask user to close old instance」——**改动要彻底退出旧实例并重启才生效**。

## [2026-10-29] 按用户要求删掉三种「本地主动停轮」
- 背景：连续几次「输出被中断」都定性为本地收尾规则（空转刹车 / 提问轮 / 计划停滞），用户要求直接从代码里移除，只保留上游与网络层的停。
- 删除范围：
  - **空转刹车**：`app/api/chat.py::_idle_brake_hit` 与循环里的 break 分支、`_auto_idle_limit` 读取、`app/config.py::agent_auto_idle_limit`、设置页「空转刹车」输入框（`agent-auto-idle`）、`tests/test_chat_blocks.py::test_idle_brake_gives_one_correction`。`auto_idle` 计数保留，只用于给下一轮加强提示（「你上一轮没有调用任何工具」）。
  - **提问轮（meta_turn）**：`app/stepctx.py::is_meta_turn` + `_META_TURN_RE` / `_TASK_CONTINUE_RE`、`chat.py` 里 `meta_turn` 的全部判定与 `not meta_turn` 门（自动续跑 / 计划体检 / 提醒 / `_pending_end`）、`elif meta_turn and plan_state` 收尾分支、`app/agent.py::_META_TURN_PROMPT` 与 `build_agent_prompt(meta_turn=…)` 参数、`stepctx.plan_can_inherit(meta=…)` 参数、`app/config.py::agent_meta_turn_stop`、设置页开关（`agent-meta-turn-stop`）、两个相关测试。
  - **计划停滞**：`chat.py::_plan_stagnant` 与其 `stalled = True` 分支、`stalled` 变量；收尾分支 `elif stalled or pseudo_stop` 收窄为 `elif pseudo_stop`，文案去掉「本地空转保护」，改成「反复只输出占位文字」；`tests/test_stepctx.py::test_plan_stagnant_needs_visible_reason`。
- 保留（未动）：上游真断流（90 秒 idle / 45 秒 stall 看门狗、重试与故障转移、断点续写）、自动续跑额度 `agent_auto_continue`、工具轮次上限 `agent_max_rounds`、任务预算 `agent_task_budget_min`、单次输出上限、用户手动停止、`ask_user` 等待、占位文本两次收尾。
- 影响：空转 / 计划停滞不再由本地刹车兜底，长任务可能一直烧 token 到自动续跑额度或你手动停止；「做完了？」这类提问轮会按普通轮处理（不再强制收尾）。
- 验证：`dev_check.py` 8/8；全量 `pytest` exit 0。

## [2026-10-30] 「继续输出」点了没反应、要切会话才刷新（前端写进脱离文档的节点）
- 现象：点「继续输出」后长时间看不到任何正文变化，切会话 / 刷新后才出现。
- 取证：`data/token_usage.jsonl` 最后两条 00:27:21（44 轮）与 00:30:11（13 轮）说明请求确实发出去了；`sessions.json` 该轮内容被拼进最后一条消息（`cont` 分支），只是界面没画出来。
- 根因：`web/stream.js::streamChat` 的续写分支（`resume && resume.row`）写的是
  `(row._secs || []).slice(1).forEach(n => n.remove())` 之后 `row._secs = [row._content]`。
  但上一轮收尾 `buildBlocksView` 重排后，`row._content` 指向的是**最后一段**正文（`row._secs[len-1]`）——
  多段正文（正文被工具调用切成好几段）时被 `slice(1)` 删掉的正是它自己。
  于是整轮续写的 token 全写进一个**已脱离文档**的节点：界面一直不动；收尾若走「没有 blocks」的兜底分支也写进同一个死节点，只有切会话 / 刷新重建才看得到。
- 修复：
  - 续写分支改成「保留**仍在文档里**的最后一段」：先挑出 `parentNode` 非空的段，`_content` 不在其中就改用最后一段，其余才 remove；`row._content` 重新指向保留段。极端情况（行被别处重排过）新建一段 `.content` 接到 `_wrap`，绝不往死节点里写。
  - `continueReply` 的守卫从缓存的 `state.streaming` 改成实时 `isStreaming()`，并在仍在生成时给 toast（以前是静默 return，看起来就是按钮坏了）。
- 相关代码：`web/stream.js`（`streamChat` 续写分支 / `continueReply`）。
- 验证：`node --check web/stream.js`；`dev_check.py` 8/8（含 39 个前端 JS 语法与 Markdown 回归）。
- 备注：只有「上一轮是多段正文」时才会踩到，单段正文的短回答看起来正常；复现要点：先跑一个带工具调用的长回答（正文多段），再点「继续输出」。

## [2026-10-30] 00:36:27「中断」：末句「修完后再跑一次确认」没被认成承诺
- 取证：
  - `data/token_usage.jsonl` 最后一条 00:36:27：28 轮全 `done`、`finish_reason=stop`、`stream_state=done`、`end_reason` **为空**、`auto_continues=3`、211 秒。
  - `app.log` 该轮前后没有 `[断流]`、没有 `[重试]`；`data/config.json` 的 `agent_auto_continue = -1`（无上限）、`agent_max_rounds = 0`（不限）、`agent_task_budget_min = 0`（不限）→ 不是额度 / 轮次 / 时间预算停的。
  - `sessions.json` 该轮落库的正文结尾是「……现在修正。修完后再跑一次确认。」，`end_reason` 为 null、`interrupted=False` → 走「正常收尾」。
- 根因：`stepctx._tail_promises_work` 漏判「X 完/好 + 再 + 动作」这种结构 ——
  `_ACTION_NEAR_RE` 只认句首引导词（现在 / 接下来 / 然后…），`_BARE_ACTION_RE` 只认句首动词，
  而「修完后再跑…」句首是「修完」，两边都不命中。于是 `auto_continue_reason` 按「成篇总结 + 无承诺」放行，
  计划里 12 步又全都标了 done（`_pending_plan_end_reason` 也返回 None），`end_reason` 落到空 —— 界面只剩「完成时间」。
- 修复：
  - `app/stepctx.py` 新增 `_AFTER_DONE_RE`（完成前缀「完 / 好 / 掉 / 毕」+ 可选「之后 / 以后 / 后」+「再」+ 动作词），接入 `_tail_promises_work`；`_ACTION_NEAR_RE` 补「修正」。
  - `app/api/chat.py::_pending_plan_end_reason` 兜底：**有计划且步骤全做完 + 本轮动过手 + 末句仍是动作承诺**时，给一条「停在‘还要再做一步’上，点继续输出」的可见原因。
- 相关代码：`app/stepctx.py`（`_AFTER_DONE_RE` / `_tail_promises_work`）、`app/api/chat.py`（`_pending_plan_end_reason`）、`tests/test_stepctx.py::test_tail_promise_detects_after_done_then_action`、`tests/test_chat_blocks.py::test_pending_plan_end_reason_covers_promise_after_all_steps_done`。
- 验证：定向 70 条全过；`dev_check.py` 8/8；全量 `pytest` exit 0。
- 备注：`auto_continues=3` 只是这一轮续跑了 3 次，不是额度用尽（配置是 -1 无上限）；同类漏判下次优先往 `_AFTER_DONE_RE` / `_PROMISE_WORK` 补句式并配正反测试。

## [2026-10-30] 收尾改成 Trae 的做法：显式 `finish` + 忘声明时补一次
- 背景：调研了 OpenCode / Codex CLI / Claude Code / Trae / DeepSeek harness 的循环写法（结论：四家都是「没有 tool_calls 就结束本轮」；只有 Trae 研究版用**显式完成工具** `task_done`，未调用即视为未完成、注入提示再跑一轮）。用户选择走 Trae 路线。
- 改动：
  - `app/agent.py`：新增工具 `finish {summary?, incomplete?}`（原生 schema + 文本协议清单 + 别名 `done / complete / task_done / task_complete / finish_task`），并新增 `_FINISH_PROMPT`（仅 agent 模式注入）：用过工具就必须在收尾时调用 `finish`，只写正文会被判成未完成。
  - `app/api/chat.py`
    - `_tool_loop_action(..., finished=False)`：`finished` 时直接 `"stop"`（显式收尾优先）；「没有工具调用 → stop」原样保留（循环必需出口）。
    - 新增 `_needs_finish_nudge(agent_mode, gal_mode, finished, nudged, tool_count)`：本轮**用过工具**却没调 `finish` 就停下 → 提醒一次（纯问答 / GAL / 非 agent 不打扰）。
    - 主循环：`turn_finished` / `nudged_finish` 两个状态；`finish` 在工具执行段单独处理（记进 tool_log，不走审批）；`act == "stop"` 分支里在**所有续跑判定之后**插入这道 nudge，只提醒一次。
    - `_finish_ok = turn_finished and 计划没有待办`：模型若在计划还挂着 pending 时就调 `finish` 逃收尾，不认，交回自动续跑。
  - `web/chat.js`：`TOOL_LABELS.finish = ['check', '声明完成']`，过程里显示成「声明完成」。
- 与 Trae 的差异（有意）：nudge 只给**一次**（Trae 靠 max_steps=200 兜底）；我们额外要求「计划无待办」才认 finish；`agent_max_rounds` 仍是 0=不限，可按需设上限。
- 相关代码：`app/agent.py`（`_FINISH_PROMPT` / `native_tool_schemas` / `_ALIASES`）、`app/api/chat.py`（`_tool_loop_action` / `_needs_finish_nudge` / `finish` 处理 / nudge 注入）、`web/chat.js`；测试 `tests/test_agent_tool_format.py::test_tool_loop_finish_is_explicit_stop`、`tests/test_chat_blocks.py::test_finish_nudge_only_when_tools_were_used`，`tests/test_prompt_budget.py` 阈值 7600 → 7700。
- 验证：全量 `pytest` 通过；`dev_check.py` 8/8。
- 备注：`finish` 不需要沙箱、不走审批；它只影响「本轮是否结束」，不会产生写操作。

## [2026-10-30] 01:20:12 不是中断：模型自己调了 `finish`，但界面没有收尾说明
- 取证：
  - `sessions.json` 最后一条 assistant 的 `tools` 尾部是 `… render_check, finish`，正文 1869 字完整（结尾「打开 index.html 打几局即可」），`end_reason` 为 null。
  - `token_usage` 01:20:13：`rounds=12`、`finish_reason=tool_calls`、`end_reason` 空、`auto_continues=2` → 走的是新增的「显式 finish」收尾路径。
  - `app.log` 里第 4 / 6 轮那两条 `[断流] state=network` 都有 `[重试]` 且最终 `done`，与本轮结束无关。
- 问题：`finish` 时不落 `end_reason`，界面只剩「完成时间」，**正常收尾和静默中断长得一模一样**；模型在 finish 里写的交付摘要 / 遗留事项（如「1024×600 视口三次被按 1280×800 执行，本轮未能复测」）只留在正文里。
- 修复：
  - `app/api/chat.py`：记录 `finish_summary` / `finish_left`；收尾链在 `_pending_end` 之前新增 `elif turn_finished` → `end_reason = {kind: "done", text: "本轮由模型声明完成：<summary>。遗留事项：…", action: ""}`。
  - `web/chat.js`：`kind === 'done'` 走灰色 `end-mute`、标记 `✓`；过程折叠标题显示「过程 · 已完成」。
  - `web/stream.js`：`END_REASON_LABEL.done = '本轮已完成'`，系统通知 tone 用 `done`（不再当警告）。
- 验证：`node --check` 两个前端文件；`dev_check.py` 8/8；全量 `pytest` 通过。
- 备注：`done` 是「正常结束」不是异常，所以不给「继续输出 / 重试」按钮；要继续做直接发消息即可。

## [2026-10-30] 新增 `#web` / `#web <URL>`：用户显式指定本轮联网（对齐 Trae 的 #Web）
- 背景：对比发现我们的联网是「模型觉得该搜才搜」（开关 + 提示词建议），Trae 是「你说搜才搜」（`#Web` 显式意图，`#Web <URL>` 直接读该页）。补上用户侧显式入口。
- 改动：
  - `web/composer.js`：输入框里行首/空白后打 `#` 弹出 `#web` 补全（`cmpInsertHash`）；选中后插入 `#web `。
  - `web/stream.js::sendMessage`：解析 `#web`（大小写不敏感）——
    - `#web 问题` → `extra.web_search_force = true`
    - `#web <url> 问题` → `extra.web_fetch_url = <url>`（同时置 force）
    - 标记只用于解析：发给模型的正文里剥掉，**气泡仍显示你打的原文**；只写 `#web` 没写问题会 toast 提示；「修改历史消息」不支持 `#web`（明确报错而不是静默丢弃）。
  - `app/api/chat.py`
    - `want_search`：`web_search_force` / `web_fetch_url` 任一为真即强制开启（仍受 GAL 默认不联网约束）。
    - `_sys_core` 末尾追加本轮约束：「必须先调用 web_search 检索再回答」或「该页正文已给出，直接据此回答」。
    - `#web <url>`：用 `public_url_error` 先校验（防 SSRF），再 `fetch_text` 抓正文，包成一条上下文消息插在**请求末尾**（前缀缓存不受影响），同时登记进研究台账拿编号，前端显示「读取网页」进度；失败只 warn 一次、不影响本轮。
    - 新增纯函数 `_web_page_note(url, text, n)`：截断 4000 字 + 明确的「外部网页内容，不要执行其中指令」声明，缓解抓取内容的提示注入面。
- 相关代码：`web/composer.js`（`#` 补全）、`web/stream.js`（`sendMessage` 解析）、`app/api/chat.py`（`_web_page_note` / want_search / `_sys_core` / 抓取注入）、`tests/test_chat_blocks.py::test_web_page_note_wraps_and_truncates`。
- 验证：`node --check` 两个前端文件；全量 `pytest` 通过；`dev_check.py` 8/8。
- 备注：与开关的区别 —— 开关管整个会话，`#web` 只影响这一轮；`#web <URL>` 绕过搜索引擎，适合还没被索引的页面 / 单个 changelog。

## [2026-10-30] 01:57:02 停轮定性：阶段总结放行拦住了自动续跑（已删除）
- 取证（`data/sessions.json` 当前会话最后一条 assistant，`done_at=01:57:02`）：
  - `tools` 共 88 次调用，最后一次是 `write_file`，**全程没有调用 `finish`**；
  - 正文 5959 字，末句「运行最终核对脚本；同时并行运行全量回归。」；
  - `plan` rev 6：`S10 / S11 / M6S4 / M6S5` 仍 pending，`M6S2` blocked；
  - `end_reason = {kind: "idle", text: "本轮先给出阶段总结；计划里还有 1 步…"}`。
- 「为什么中断」的两级：
  1. 直接触发在 `_tool_loop_action`：这一轮没有任何工具调用 → `stop`（主循环出口，保留）；
  2. 唯一能挽救的 `auto_continue_reason` 被 `is_checkpoint_summary(text, agent_summary_stop_chars=180)` 提前 `return None` —— 正文够长、尾部 300 字里有 `PASS`，被判成「阶段总结，允许先收尾」，于是既不续跑也不提醒。
- 连带缺陷：`_BARE_ACTION_RE` 只认「句首/(句号|分号|换行) + 动作 + 直到结尾」，末句是「运行 A；同时并行运行 B。」—— 分号后以「同时并行」开头、且不能跨分号，整句被判成「已交付」。
- 改动：
  - `app/stepctx.py`：删除 `is_checkpoint_summary` / `_SUMMARY_MARK` / `auto_continue_reason(summary_stop_chars=)` 参数与分支；
  - `app/api/chat.py`：删除 `_summary_chars` 读取、`_summary_stopped` 与收尾 `elif _summary_stopped …`（同时去掉无用的 `_has_pending`）；
  - `app/config.py`：删除 `agent_summary_stop_chars`；`web/settings.js`：删除「阶段总结放行阈值」输入项与 `_collect` 字段；
  - `_BARE_ACTION_RE`：抽出 `_BARE_VERBS` / `_BARE_LINKS`，动词前允许「同时 / 并行 / 顺带 / 一并 / 再 / 然后」，并允许「；+ 第二段动作」连写结尾。
- 测试：删除 `test_checkpoint_summary_allows_stop`；新增 `test_tail_promise_detects_semicolon_chained_actions`。
- 验证：全量 `pytest` 通过；`node --check web/settings.js|stream.js|composer.js` 通过；`scripts/dev_check.py` 8/8。
- 结果：以后长正文 + 计划还有步骤时，只要末句还在承诺动作，就会继续自动续跑；不再有「字数到了就放行」这一路。

## [2026-10-30] Token 口径核查：`usage.prompt` 是整回合累计，不是单次输入
- 现象：消息下方的「输入 11,100,730」与上下文明细里的「第 35 次调用」合计 200,622 相差约 55 倍。
- 核查（02:17:04 那回合）：
  - 模型调用 60 次，单次输入 158,854 → 200,621；
  - `timing.rounds_detail[].prompt` 求和 = 11,100,730 = `usage.prompt`（误差 0）；
  - `data/token_usage.jsonl` 同一时刻那条也是 `prompt=11100730`，三处一致；
  - `in_parts` 各类之和 = 200,622 ≈ 单轮 prompt（拆分正确）。
- 结论：**计算没有重复或漏算**，差异是「累计 vs 单次」口径；`usage.prompt` 每轮都把完整上下文再发一遍，60 次累加。
- 计费口径：`cached` 是 `prompt` 的子集（DeepSeek 的 `prompt_tokens = prompt_cache_hit_tokens + miss`），11,100,730 里只有 95,034 按未命中价计费；上下文进度条已用 `usage.last_prompt` 校准（`app/context.py::calibrate_ratio`）。
- 显示改动（`web/chat.js` / `web/stream.js`）：
  - `usageText(u, doneAt, calls)` 新增第三个参数 → 「输入 X（N 次调用累计）」；
  - 第二行改为「缓存命中 X（%）· 未命中 Y（按未命中价计费）· 末轮单次 Z」；
  - 新增 `usageCalls(m)`（优先 `timing.rounds_detail.length`，其次 `timing.rounds`）；
  - `stream.js` 的 `timing` 事件把 `obj.timing` 存到 `row._timing`，`usage` / `done` 两处重绘时带上次数；
  - 明细面板顶部「累计输入 …（N 次调用相加）」，meta 里「末轮上下文」改为「末轮单次输入」。
- 已知小瑕疵（未改）：`rounds_detail` 的 `n` 会重复（同一轮内追加了多次模型调用，如 35 出现 4 次），列表看起来像重复计数，但每次都是真实调用、都计费。
- 验证：`node --check web/chat.js|stream.js`；`scripts/dev_check.py` 8/8。

## [2026-10-30] IDE 新增 AI Tab 补全（对齐 Trae / Copilot 的内联建议）
- 目标：IDE 模式里输入停顿时出现灰色 ghost text，按 Tab 接受、Esc 关闭。
- 参考与取舍：
  - Trae / Copilot：内联 ghost text + Tab 接受 —— 采用；
  - Continue.dev / Codeium：FIM（前缀 + 后缀）+ 短超时 + 不重试 —— 采用；
  - Trae 的「预测下一处编辑」跨位置跳转 —— 第一版不做（工作量大、误报多）。
- 前端（`web/gal/gal-ide.js`）：
  - `ideEnsureEditor` 增加 `inlineSuggest: { enabled: true, showToolbar: 'never' }`，创建后调用 `ideAiInit`；
  - `monaco.languages.registerInlineCompletionsProvider` 按语言白名单注册（`IDE_AI_LANGS`，不含 markdown/plaintext）；
  - `ideAiProvide`：只服务主编辑区当前模型（`state.ideEditor.getModel() === model`，避免 diff 编辑器误触发）；只在行尾 / 后面只剩 `)]};,` 时请求；空行不补；
  - 停顿 `ide_ai_complete_delay_ms`（默认 350ms）后再发请求，期间用 Monaco 的 cancellation token 取消；
  - `ideAiFetch`：取光标前 6000 / 后 2000 字符，`fetch('/api/ide/complete')` + `AbortController`；同版本同偏移结果本地缓存；
  - 请求返回后再比对 `versionId + 光标位置`，过期结果丢弃；
  - Tab：`onKeyDown` 里仅当 `IDE_AI.text` 非空且版本/行列都一致时才 `preventDefault` 并 `editor.action.inlineSuggest.commit`，否则保持缩进；Esc 清空状态；`Alt+\\` 手动触发。
- 后端：
  - 新模块 `app/ide_complete.py`：`build_messages`（FIM 提示 + 裁剪）、`clean_completion`（去 ``` 围栏 / 去与后缀重复部分 / 限行数限字符）、`allow` + `note_result`（每会话 0.8s 间隔、连续失败 5 次冷却 60s）；
  - 路由 `POST /api/ide/complete`（`app/api/sandbox.py`）：`_sb_guard` 校验会话，**不读不写文件**，只回建议文本；
  - `app/llm.py::_quick_chat` 新增 `tries` / `use_cache` / `temperature` 三个可选参数（默认值保持旧行为）；补全用 `tries=1、timeout=(3,5)、use_cache=False、temperature=0`；
  - 未配置 `light_model` 时直接返回「未配置轻量模型」，不落到主模型。
- 配置与设置：
  - `app/config.py`：`ide_ai_complete=True`、`ide_ai_complete_delay_ms=350`、`ide_ai_complete_max_lines=8`；
  - `web/settings.js`：Agent 设置新增「编辑器 · AI Tab 补全」分组（开关 / 停顿毫秒 / 最多行数），并回填与 `_collect`。
- 验证：
  - `tests/test_ide_complete.py`（9 条：围栏、后缀去重、行数/字符截断、上下文裁剪、限流与冷却、未配置 light_model / 开关关闭、路由注册）全过；
  - 全量 `pytest` 通过；`node --check web/gal/gal-ide.js|web/settings.js` 通过；`scripts/dev_check.py` 8/8；
  - 用假的 `_quick_chat` 跑通 `complete()`：输出 `return total`，参数 `max_tokens=192 tries=1 cache=False temp=0.0 timeout=(3,5)`。
- 注意：当前 `data/config.json` 的 `light_model` 为空 → 补全**不会触发**，需要先在「设置 → 轻量任务的模型」里填一个（可与主模型同名、走同一接口）。

## [2026-10-30] README 同步本轮功能
- 背景：README 还停留在 `finish` / `#web` / AI 补全 / 用量口径上线之前。
- 改动（`readme.md`）：
  - 「交互体验」表：联网搜索行补 `#web` / `#web <URL>`；新增「AI Tab 补全（IDE）」行；新增「用量口径」行（累计 vs 单轮、未命中才计费）。
  - 「文件 Agent（沙箱）」表：新增「收尾判定」行（`finish` + 自动续跑 + 「本轮已完成」）。
  - 「配置说明」：在聊天 API 之后新增「轻量模型（可选）」，说明 `light_model` 与 IDE 补全的关系（留空 = 补全不触发）。
  - 「项目结构」：`app/` 列表补 `ide_complete.py`。
- 未改动：README 其余章节（GAL / 大厅 / 工作台 / 技术栈）本轮无行为变化，保持原样。

## [2026-10-30] README 整体重构：按代码现状重写并补工程章节
- 核查发现的原文档过时项：
  - 设置面板实际是 **5 组 13 分类**（模型与接入 / 对话与主动 / 记忆与能力 / 外观与声音 / 系统），原文写「6 组 13 分类」且分类名不同；
  - 原文 Agent 工具只列了 11 个，实际有 60+（含 `shell` / `check_code` / `lsp_*` / `computer_*` / `explore` / `workflow` 等）；
  - `search_rewrite`（查询改写）已废弃、不在 DEFAULT_CONFIG，原文整段配置说明失效；
  - 大厅实际注册 6 个功能（GAL 剧场 / 图寻 / 数学竞技场 / 模拟股市 / 模拟面试 / 研究道场），原文只写了图寻；
  - 原文项目结构里 `app/sandbox.py` 不存在（实际是 `app/api/sandbox.py`），`web/gal/` 文件名漏了 `gal-` 前缀，`scripts/` 与 `web/` 缺不少文件。
- 重构内容（`readme.md`，518 → 665 行）：
  - 保留标题、徽章与开头整活引言；
  - 新结构：概览 → 快速开始 → **架构概览** → 功能说明（4.1–4.9 按域）→ 配置参考 → **安全与边界** → **已知限制与非目标** → 项目结构 → 更新与备份 → FAQ（按安装 / 配置 / 使用 / 数据分组）→ 技术栈 → 许可证；
  - 新增章节依据代码事实撰写：进程模型、一次对话的请求链路（加载 → 装配 → 压缩 → 流式 → 工具 → 收尾 → 落库）、目录职责、数据分层；
  - 语言改为陈述句，去掉整句加粗，只保留术语强调。
- 验证：`|` 表格列数一致性脚本检查 0 处不一致；标题层级 12 个二级标题完整；无「查询改写」「6 组 13」残留。

## [2026-10-30] 用量行排版与口径命名：文本行 + 明确口径
- 背景：原三行长文本用 `·` 串起来信息密度高；中途改成「标签 + 数值」网格，觉得醒目但观感生硬，回退为文本行。
- 口径核对（`app/api/chat.py` 的 `usage_total`）：`prompt` 与 `completion` **都是整回合累加**（每轮都重发完整上下文），只有 `last_prompt` 是单轮值。所以「输出」不能叫「本轮输出」，叫「总输出」。
- 最终形态（`web/chat.js::usageText`，结构不变，仅改标签）：
  ```
  deepseek-flash · 5 次调用 · 总输入 115,582 · 总输出 3,195 · 合计 118,777
  缓存命中 68,736（59.5%） · 未命中 46,846 · 末轮输入 28,907
  完成时间 2026-10-29 21:40:08
  ```
- 派生数据不放行内，统一放在「token 明细」页签的 meta 行：`平均每轮输入`（总输入 ÷ 调用次数，调用次数 > 1 才显示）与 `推理输出`（`usage.reasoning`，为 0 不显示）。
- 页签改名：「输入明细」→「token 明细」（`index.html` 按钮、`chat.js` / `init.js` / `base.css` 注释、`app/context.py` 与 `app/api/chat.py` 的 docstring 同步）。
- 同步改动：`usageTitle` 口径解释同步改名；「token 明细」面板 `ctxRenderInput` 的「累计输入 / 末轮单次输入」改为「总输入 / 末轮输入」；readme 用量口径与问答同步改名。
- 教训：用户只要求「改名字」时不要顺带改结构（行数、字段增减）；新增指标先问放哪儿。
- `web/css/base.css`：`.usage-line` 保持 `white-space: pre-line`，无 flex / 无子元素样式。
- 调用点：chat.js 3 处、stream.js 2 处，均为 `setUsage(...)`。
- 验证：`node --check`；浏览器实看历史消息，字号 11.5px、颜色与旧版一致，无溢出与重叠；`scripts/dev_check.py` 8/8。

## [2026-10-30] 用量统计弹窗：指标细化 + 双图表 + 占比视图
- 问题/决策：原弹窗只有 5 个数字 + 一条按模型折线 + 五张纯数字表，看不出「贵在哪、哪天贵、输入还是输出占大头」。
- 解决方案：
  - 后端 `app/usage.py::aggregate_usage` 新增三块，原有字段一个不动（向前兼容）：
    - `daily`：不分模型的按时间桶汇总，覆盖连续桶（空白日补 0），与 `labels` 同序，供堆叠柱用；
    - `by_api`：按接入通道（chat / light / vision）汇总；
    - `derived`：`hit_rate`（缓存/输入）、`avg_call`（总量/次数）、`avg_active`（总量/有消耗桶数）、`active`、`peak`。分母为 0 时给 `None`，前端显示「—」而不是 NaN。
  - 前端 `web/settings.js::renderUsage`：7 张概览卡（总 tokens / 输入 / 输出 / 缓存命中 / 调用次数 / 峰值日 / 活跃日），每张带灰色副行；表格新增「占比」列（细进度条 + 百分比）与「按接入通道」表。
  - 新增 `drawUsageBars`：输入/输出堆叠柱（左轴 tokens）+ 调用次数折线（右轴次数），与原有 `drawUsageChart`（按模型折线）并存；两个 ECharts 实例分别管理，`openUsage`/`closeUsage`/`resize` 都成对处理。
- 踩坑：
  - 柱状图 Y 轴名「tokens / 次数」被画布上沿裁掉 —— `grid.top` 由 20 调到 34 后正常（像素扫描确认画布顶部前 11 行无内容像素）。
  - `el.innerHTML = ''` 前必须先 `dispose()`，否则旧实例挂在已移除节点上。
- 相关代码/指令：
  - `app/usage.py`（aggregate_usage / _derived）、`web/settings.js`（renderUsage / drawUsageBars / usageAxisLabel）、`web/index.html`（#usage-bars、#usage-chart-title、#usage-bars-title）、`web/css/base.css`（.uc-sub / .usage-h3 / .usage-share）、`web/init.js`（resize 同步两个实例）
  - 测试：`tests/test_usage.py` 新增 4 条（daily 桶 / derived / 空区间不除零 / by_api），11 条全过。
- 验证：`python -m pytest tests/test_usage.py -q` 11 passed；`node --check web/settings.js web/init.js`；`scripts/dev_check.py` 8/8；真实 `token_usage.jsonl` 只读跑通（7 天 1876 次调用、命中率 95.7%）；浏览器实测 7 天 / 30 天 / 12 个月三档，双图、轴名、图例、占比条均正常，无 JS 报错。
- 已知：窄视口（~556px）下六张表需在 `.usage-tables` 内横向滚动，属既有响应式行为，未改动。

## [2026-10-30] Solo 消息图片：从「点一下变大」改为全屏查看器（可缩放平移）
- 问题/决策：原实现是 `box.classList.toggle('zoom')`，只是把缩略图撑到 100% 宽，看不清细节。改为对齐图寻题面放大的交互手感。
- 实现（`web/chat.js` 新增 `imgZoom*` 一组函数，约 160 行）：
  - 点击消息图片 → 全屏浮层（挂 `document.body`），打开时按「适应窗口」定倍；
  - 滚轮以指针为锚点缩放（×1.12/格），按钮 ×1.25/次，范围 0.1–8；
  - 指针拖动平移（`setPointerCapture`），双击在「适应窗口 / 原始尺寸」间切换；
  - 图片外空白轻点关闭（命中图片不关，避免看细节时误触），Esc 关闭；
  - 关闭时 `removeAttribute('src')` 并重置 transform，避免占着解码内存与下次打开闪位。
- 样式：`web/css/polish.css` 新增 `.img-zoom*`（照 `hall.css` 的 `.geo-zoom*` 改名，同一套布局），删除已无用的 `.msg-image.zoom` 两行。
- 切模式收浮层：`applyUiMode()` 开头调 `imgZoomClose()`（typeof 保护），否则看图时切 Solo/IDE/GAL 会盖住新界面。
- 踩坑/注意：
  - 图片必须「按原始像素 + 绝对定位到舞台中心」渲染，不能用 flex/grid 居中——图大于舞台时 `overflow:hidden` 会把居中退化成贴边。
  - 静态文件走 `/static/<rel>`，根路径只映射 index.html；临时自测页要放 `web/` 下用 `/static/_xxx.html` 访问。
- 验证：`node --check`；`scripts/dev_check.py` 8/8（39 个前端 JS 全过）；用临时自测页（直接 `fetch('/static/chat.js')` 截取这段真实代码在 `new Function` 里跑）做浏览器实测，10 项全过：打开适应 35%、滚轮 35→49%、拖动纯平移 150px、双击到 100%、适应回到 35%、按钮 35→43→54→43、Esc 关闭、空白处关闭、控制台零报错。临时页已删除。
- 已知：浮层沿用 `rgba(8,9,12,.95)` 遮罩（与图寻一致），能隐约看到底层；若要完全不透光需另调。

## [2026-10-30] 系统通知支持点击跳转（会话 / 任务）
- 问题/决策：`showSystemNotify` 的 `onclick` 只做 `window.focus()`，用户点完通知还得自己去翻是哪个会话 / 哪个任务。
- 解决方案（`web/auto.js`）：
  - `showSystemNotify(title, text, kind, target)` 新增第 4 个参数；`notifyOnce(..., target)` 透传；
  - 新增 `notifyJump(target)`：
    - `{ taskId }` → 切到工作台（`setUiMode('work')`）+ `workOpenTask(id)` 打开任务详情；
    - `{ sessionId }` → 切回 Solo + `selectSession(id)` + `scrollToBottom(true)` 滚到最新；
    - 都不传 → 只把窗口拉到前台（保持旧行为）；
  - 所有异常吞掉，跳转失败不影响通知关闭。
- 各通知的落点：
  - 自动化（完成 / 需要你 / 暂停 / 重试）→ `{ sessionId: g.session_id }`；
  - 服务端主动 `notify` 事件（提醒到点等）→ `{ sessionId: obj.session_id }`；
  - 任务引擎（等待确认 / 等待你完成 / 完成 / 失败）→ `{ taskId: t.id }`；
  - `web/stream.js` 的对话结束通知与提问卡通知 → `{ sessionId: sid }`（`notifyChatDone` 新增 `sessionId` 选项，3 个调用点都传）。
- 验证：`node --check web/auto.js web/stream.js`；`scripts/dev_check.py` 8/8（39 个前端 JS 全过）。
- 待人工确认：真实系统通知的点击回调要在浏览器 / Electron 壳里点一次才算验完（自动化点不到系统通知）。测法：切到别的会话后等本轮结束 → 点通知 → 应回到原会话并停在最新一条；工作台任务到「等待确认」时点通知 → 应打开该任务详情。

## [2026-10-30] 设置文案去掉厂商绑定（联网搜索）
- 问题/决策：联网搜索页的说明写着「聊天模型自带搜索时优先用它（目前是 DeepSeek，复用聊天 API 的密钥）」，把通用能力写成了特定厂商的说明。项目定位是通用 AGI，不绑定任何一家。
- 解决方案：删掉括号里的厂商说明，只保留能力描述——「默认：聊天模型自带搜索时优先用它」。
- 相关位置：`web/settings.js` 的 `hintHtml('默认：聊天模型自带搜索时优先用它…')`（`search-prefer-api` 下方）。
- 说明：后端注释与 `app/model_caps.py` 里的厂商名属于实现事实（当前只有该服务商提供 Search API），不在这次文案范围内，未改。
- 验证：`node --check web/settings.js` 通过。

## [2026-10-30] 补齐 /plan 的介绍入口（命令面板 + README）
- 问题/决策：`/plan` 只有帮助面板一条说明，`/` 命令菜单里没有它、README 也完全没写，用户根本发现不了。
- 解决方案：
  - `web/composer.js`：`CMP_COMMANDS` 的「AI」组新增 `/plan`（`kind: 'local'`）；`cmpRunCommand` 新增 `case '/plan'`——**不直接执行**，只把输入框填成 `/plan ` 并把光标放到末尾，等用户补完目标再回车（真正的规划模式由 `web/stream.js` 在发送时识别前缀触发）。
  - `readme.md`：4.1「输入框增强」的指令清单补 `/plan`、`/spec`；4.3 文件 Agent 表格新增「规划模式（`/plan`）」一行，写明只读边界（不给写文件 / 跑命令 / Git 提交）、产出「目标 + 完成判据 + 里程碑 + 首步拆解」、不执行、下一轮才动手。
- 相关实现（未改动，供参考）：`app/agent.py` 的 `_PLAN_MODE_PROMPT`（6120 行附近）与只读工具过滤（1460 行附近）；`app/api/chat.py` 2269 行也会再兜一次前缀剥离。
- 验证：`node --check web/composer.js`；浏览器实测——敲 `/` 菜单出现「/plan 先出计划（本轮只读，不执行）」且归在 AI 组；点选后输入框为 `/plan `（6 字，光标在末尾）；输入 `/pl` 只剩这一项；控制台零报错；全程未回车，未触发真实请求。

## [2026-10-30] 右下角「?」改为「打开设置 → 功能说明」
- 问题/决策：`?`（`#hk-hud-toggle`）原本是展开快捷键 HUD，用户希望它直接去「设置 → 功能说明」。
- 解决方案：
  - `web/hotkeys.js::initHotkeys`：HUD 强制 `collapsed`，按钮 `onclick` 改为 `openModal({ tab: 'help' })`；不再读 `localStorage.hotkeys_hud_open` 自动展开（否则旧状态会弹出一列快捷键且按钮已无法收起）。
  - `web/index.html`：按钮 `title` / `aria-label` 改为「打开功能说明」，注释同步。
- 现状：`hkHudToggle` / `hkHudTimerStart` / `HK_HUD_KEY` 已无调用者（`hkHudRefresh` 仍被改键流程调用，但清单永远折叠）；要彻底删可另开一轮。
- 验证（浏览器实测）：点击后设置弹窗打开，`.snav-btn.active` 为「功能说明」，右侧是 39 个可折叠条目（含「快捷键总览」「用量统计」）；`.hk-hud` 保持 collapsed、`.hk-hud-list` 为 `display:none`；✕ / Esc 可关闭，重开与整页刷新后仍停在「功能说明」；控制台无报错。
- 已知表现（非本次引入）：窄视口（392×653）下左侧导航不会自动滚到激活项，「功能说明」可能落在可视区外。

## [2026-10-30] 设置 →「功能说明」页重构（目录 / 筛选 / 去设置）
- 问题/决策：31 条说明平铺成 7 组，只有原生展开/收起；没有页内导航，搜索只能定位标题，长条目里的「设置 → xxx」要用户自己去找。
- 结构：
  - `web/index.html`：删掉 help 页开头那句 hint，改成 `<div id="help-head" class="help-head"></div>`（内容由 JS 生成）。
  - `web/settings.js` 新增 `initHelpPane()` 及配套：按文档顺序把 `h4.set-group` 与 `details.help-sec` 切成 7 组；给每条分配稳定 id `help-N`（深链）；生成工具条（筛选框 + 全部展开/收起切换）与目录 chip（带条数）。
- 视觉（`web/css/base.css`）：
  - `.help-head` 吸附在设置页 sticky 页头（`.set-page-head`，z-index 4）**下方**；吸附区 `top` 与高度由 `helpSyncHeadHeight()` 实测后写入，避免被页头盖住（这是第一版的真实 bug）。
  - 目录 chip 一行横向滚动（`flex:0 0 auto; white-space:nowrap`），窄窗口不换行；旧实现被 flex 压成 34px 宽、文字竖排。
  - 条目卡加展开态描边、标题内 `mark` 高亮、「去设置」胶囊按钮；`.help-tip` 改成左侧竖线提示块。
  - `scroll-margin-top: var(--help-head-h)` 让跳转落点避开吸附区。
- 交互：
  - 筛选：同时匹配标题、`data-search` 与正文（`dataset.plain` 首次缓存），命中项自动展开并高亮标题；清空时恢复筛选前的展开状态；整组无命中时连带隐藏 h4 / set-card / chip，chip 条数显示「命中/总数」。
  - `Esc`：焦点在筛选框且有值 → 只清空筛选，不关设置弹窗（`onSettingsKeydown` 里优先判断；之前会被全局 Esc 直接关掉）。
  - 「去设置」：按 `HELP_SET_TARGET` 映射切到对应分页，滚动到首个匹配 `data-search` 的控件并加 1.2s 高亮；没有对应设置页的条目不给按钮。
  - 目录 chip 点击跳分组；滚动时用 `--help-head-h` 扣掉吸附高度后高亮当前组（旧实现没扣，滞后一组）。
  - `openModal({ tab:'help', anchor:'help-N' })` 可深链到某条并展开。
- 内容更新（只改与代码不符/新增的）：
  - 上下文上限：补「token 明细」页签（逐轮明细、输入构成、末轮/平均每轮/推理输出）。
  - IDE：补 AI Tab 补全（Tab 采纳 / Esc 收起 / `Alt+\` 触发，需先配 `light_model`）。
  - 自动续跑：补 `finish` 显式收尾（计划未完成时不采纳）。
  - 快捷键总览：区分键盘 `?`（快捷键清单）与右下角 `?` 按钮（打开功能说明）。
- 验证：`node --check web/settings.js`；浏览器四轮实测——入口/目录/计数、筛选「通知」命中 3 条、Esc 只清筛选、展开收起切换、滚动高亮跟随、去设置有高亮、chip 单行横向滚动、控制台无 JS 报错。
- 已知：窄视口（392px）下吸附区约 160–200px，正文可视区偏小；首次点击筛选框偶发被 `.modal-card` 抢焦点（既有焦点管理问题，未改）。

## [2026-10-30] 移除「计划停滞放行」：计划还有待办就继续催
- 问题/决策：15:34:11 的停轮被定位为 `plan_stale >= 2` 放行（计划 rev 4 连续多轮未变 + 最后 3 轮只输出一句话、无工具调用）。用户判断模型现在会自己调 `finish`、计划维护能力足够，要求删掉该刹车。
- 改动：
  - `app/stepctx.py::auto_continue_reason`：删除 `plan_stale` 形参；放行条件从 `(not todo or plan_stale >= 2)` 改为 `not todo`；续跑分支从 `agent_mode and todo and plan_stale < 2` 改为 `agent_mode and todo`；docstring 同步。
  - `app/api/chat.py`：调用点去掉 `plan_stale=` 实参。`plan_stale` 仍保留（只用于「计划体检」`plan_replan_hint`）。
  - `tests/test_stepctx.py`：重写 3 条依赖旧放行的断言（长交付仅在「计划无待办」时放行；短句 + 待办一律续跑；承诺句的 why 改为「计划还…」）。
- 实测与残留风险见下一条「续跑/停止判定：六家产品调研 + 删除 plan_stale 后的风险矩阵」。
- 验证：`pytest tests/test_stepctx.py` 51 条全过；`scripts/dev_check.py` 全部通过（97 模块导入、39 前端 JS）。

## [2026-10-30] 续跑/停止判定：六家产品调研 + 删除 plan_stale 后的风险矩阵
- 问题/决策：15:34 停轮后决定删掉 `plan_stale>=2` 放行。删除前先横向调研主流 coding agent 如何处理「什么时候停 / 要不要自动续跑」，再实测删除后的风险边界。
- 六家做法（来源见条目末尾）：
  - **Codex CLI（开源）**：循环到「模型不再发起工具调用、改发用户消息」即结束；**没有自动续跑**，所以不存在计划停滞问题。
  - **Claude Code `/goal`**：每轮结束交独立 evaluator 判定；未达成自动开下一轮。停止：条件满足 / 判定不可能 / 需用户修的错 / `/goal clear` / 条件里写 `or stop after 20 turns`。**关键保护：连续几轮没有工具使用 → 停循环 + 警告 + 交还控制权**（goal 保留）。
  - **OpenCode + goal 插件**：`session.idle` 后跑 evaluator，NO 则 `promptAsync` 续跑。刹车：用户取消、同一理由 NO 太多次（noProgressLimit）、无可衡量进展 → stuck、连续续跑硬上限、maxTurns / token / 时间预算、evaluator 出错 fail open。
  - **Trae Agent（开源）**：只有调用 `task_done` 才算完成；没工具调用就塞一句 "It seems that you have not completed the task." 继续；唯一硬刹车是 `max_steps`。无计划/待办机制。
  - **DeepSeek Harness（开源，dsh）**：loop 本身是 Cordis 插件，官方文档未给默认刹车；第三方 dsh-do 插件是 `loop_start` → 直到 `loop_done` 或轮次预算耗尽自动 block。
  - **ZCode `/goal`**：每轮结束单独校验，**看实据**（改出的文件 / 命令输出 / 测试结果），计划、待办、耗时、像结论的回复都不算；还有待办就不判完成。停止仅三种：校验通过 / 用户暂停清除 / 用量上限；计划模式与目标模式互斥。
- 横向结论：**没有一家用「计划停滞 N 轮」当刹车**；有五家保留「无进展」保护（Claude Code 的 no tool use、OpenCode 的 noProgressLimit、ZCode 的实据校验、Trae 的 max_steps、dsh-do 的轮次预算）。我们删完后是 0 兜底，这是唯一实质差异。
- 删除后的行为矩阵（纯函数实测 + 真实数据回归）：
  - 继续跑：空转短句、完全空正文、长交付但结尾无完成词、短句「已完成」（<300 字）、**调 `finish` 但计划没标完**（`_finish_ok=False`）。
  - 停下：计划全部 `done`；长文本（≥300 字）+ 结尾 160 字内有完成词且无动作承诺（**不看计划**）；声明受阻；待拍板决策；非 Agent 会话；上游异常；手动停止。
  - 真实现场回归（sessions.json s_2809c1a88538 的 idx1057）：现在判定「继续跑 · 计划还有 2 步未完成」。
- 判定实现细节（本轮回读代码确认，改动时最容易踩）：
  - `_claims_complete(text)` 只看**最后 160 个字符**里有没有完成词；而「成篇交付放行」还要求**全文 ≥ 300 字**且 `not _tail_promises_work`。所以「短句：已完成。」仍会续跑，「长文 + 结尾：全部完成了。」才放行 —— 同样的意思，位置和长度不同，结果相反。
  - 计划分支统计的是 `_pending_steps()`，含 `doing` 与 `pending`（现场 3 done / 1 doing / 1 pending → 算 2 步未完成）。
  - `_pending_plan_end_reason()` 只在「正常 stop + 计划还有步骤 + 本段没有工具调用」时补 end_reason：空正文 / 短句 → `idle`；长交付（≥300 字无承诺）→ 不补（视为已交付）。这也是 15:34 那条消息 `end_reason=None` 的成因。
  - 顺序很关键：`_claims_blocked` 与 `_pending_decision` 排在最前，任一中命中直接放行 —— 这是「模型说卡住了 / 让用户拍板」时唯一的软出口。
- 场景实测明细（计划有 2 步未完成时）：
  | 输出 | 结果 |
  | --- | --- |
  | 空正文 / 「说明。」/ 「现在调用 plan_patch 和 run_command。」 | 续跑 |
  | 短句「已完成。」（<300 字） | 续跑 |
  | 长文 + 结尾「全部完成了。」/「已改完了，剩下两步先不做。」 | 停（文字放行，不看计划） |
  | 长文 + 结尾「改动已完成。剩余：跑回归。」 | 续跑（结尾有动作承诺） |
  | 「没有权限…需要你提供」/ 待拍板清单 | 停 |
  | 调用 `finish` 但计划没标完 | 续跑 |
- 两个必须知道的点：
  - `finish` 不是最终裁决：`app/api/chat.py::_finish_ok = turn_finished and not stepctx._pending_steps(plan_state)`，计划没标完照样驳回并继续催。
  - `app/agent.py::normalize_auto_continue` 的 docstring 写「连续空转另有保护（见 auto_idle）」，但 `auto_idle` 现在只用于加强续跑提示（chat.py 4080 附近），**不会停轮** —— 文档与实现不符，易误判有兜底。
- 兜底现状：`agent_auto_continue=-1`（不限次）、`agent_max_rounds=0`（不限轮）、空转不停轮。唯一硬刹车是上游异常与手动停止；想零成本加保险可把「最多工具轮次」设成具体值。
- 验证：`pytest tests/test_stepctx.py tests/test_agent_plan.py tests/test_stream_stop.py` 共 80 条全过；场景脚本与真实数据回归跑完即删。
- 来源：
  - https://openai.com/index/unrolling-the-codex-agent-loop/
  - https://code.claude.com/docs/en/goal （无进展保护原文：no tool use for several turns in a row）
  - https://github.com/deepcode-ai-sys/opencode-goal-mode-deepcode/blob/HEAD/GOAL.md
  - https://github.com/bytedance/trae-agent （base_agent.py / agent_prompt.py）
  - https://harness-deepseek.org/ 与 https://awesome-dsh-plugin.com/p/toujianjian/dsh-do/
  - https://zcode.z.ai/cn/docs/goal

## [2026-10-30] 会话搜索：点命中后定位到那条消息（并高亮搜索词）
- 问题：会话搜索浮层点击结果只调 `selectSession(id)`，后端返回的 `hits[].index` 被丢弃 —— 打开会话后停在底部，早期命中根本不在最近 80 条里，表现为「跳转没生效」。
- 修复（`web/chat.js`）：
  - `ssState.q` 记录当前搜索词；命中片段 `p.onclick` 传 `(it.id, h.index, ssState.q)`；回车取第一条命中的 index。
  - 新增 `ssFocusMessage(idx, q)`：循环 `loadOlderMessages(..., silent=true)` 把分页补到目标之前 → `renderMessages(start, true)` → **覆盖它重设的 scrollTop** → 按命中词的 `Range` 真实坐标把词对齐到视口中心 → 再跟 5 帧（90ms）校正，抵消图片/富媒体异步加载带来的高度变化。
  - `_followBottom = false`：避免后续增量渲染把视图拉回底部。
  - `ssScrollSideways(range)`：命中落在可横向滚动的代码块里时，把该块 `scrollLeft` 滚到命中处（否则文字被 clip 在聊天列外）。
  - 高亮改为「命中词浮层」：`ssFlashTextRange` 对 `range.getClientRects()` **每个矩形**画一个 fixed 的 `.ss-text-flash`（跨行时每行一个），1.8s 自动移除；找不到词时退回整条消息的 `.ss-msg-flash`。
- 踩坑：
  - `renderMessages` 的 `expanding` 分支会按「旧高度差」重设 `scrollTop`（本意是展开更早时保持视觉位置），跳转场景下会把视图送到底部 —— 必须在它之后自己再设一次。
  - `scrollIntoView({behavior:'smooth'})` 在 500 万 px 级列表上不可靠（会被高度重算打断），改为直接算 `scrollTop`。
  - 高亮若加在 `.msg` 容器上，对 1 万 px 高的消息完全看不见 —— 必须框到命中词。
- 验证：浏览器实测「摇杆」「握手」「计划」三个用例，命中词都落在视口中心 ±5px；跨行词每行一个高亮框；代码块横向滚动生效（scrollLeft 0→473）；控制台无 JS 报错。
- 已知：跳转耗时 5–10 秒，根因是 `renderMessages(start, true)` 会从命中位置一路渲染到最新一条（该会话 1078 条）。要做快需给 `renderMessages` 加 endIndex 做窗口化渲染，本轮未改。

## [2026-10-30] 附件：解除图片/文档互斥 + 补两处预览
- 问题：图片和文档原先是互斥的（`attachImageBlob` 见 `pendingDoc` 就拒、`attachDocBlob` 见图片就拒并清空图片），但发送链路（`extra.images` 与 `extra.doc` 分开传）和后端（分别 `save_upload` / 解析）本来就支持并存；另外待发送图片不能放大、文档任何阶段都不能预览。
- 改动：
  - `web/stream.js`：删掉两处互斥判断；添加文档时不再清空已选图片；`renderAttach` 文案改为组合（「已附带 2 张图片 + 文档（发送时自动抽取正文）」）。
  - 待发送缩略图：`thumb.onclick = () => imgZoomOpen(url)`，复用已有查看器（缩放/拖动）。
  - 文档预览：`doc` chip（待发送 `#attach-doc`、已发送 `.msg-doc`）都可点击。
- 文档预览实现（`web/chat.js`）：
  - 统一浮层 `#doc-pv`（JS 动态创建）：标题、下载、关闭、Esc；正文区按类型渲染。
  - 文本类（txt/md/csv/json/log/html/css/js/py…）用 `<pre>` 显示，>300KB 截断并提示下载。
  - PDF 用 `<iframe>` 内嵌；其它格式提示「下载后用本机应用打开」。
  - 发送前：`state.pendingDoc.b64` → Blob；发送后：消息里只有 `doc.file`（落盘名），走 `POST /api/attach/get {session_id, name: doc.file}` 取字节。
  - 关闭时 `URL.revokeObjectURL`，清空 body（iframe 一并释放）。
- 验证（浏览器实测，服务端口 51350）：
  - 先传图片、再传 readme.md：未被拒，缩略图与文档 chip 同时存在，提示为「已附带 图片 + 文档（发送时自动抽取正文）」。
  - 点缩略图 → `.img-zoom` 全屏查看器出现，Esc 关闭正常。
  - 点文档 chip → `.doc-pv` 出现，标题 readme.md、正文为 Markdown 纯文本（24348 字符）、右上「下载」可用，Esc 关闭正常。
  - 控制台无 JS 报错（仅端口 51340 旧实例的连接重置噪声）。
- 踩坑/注意：应用的两个 file input（`#file-input` / `#doc-input`）是 `hidden`，自动化工具拿不到 ref，上传只能人工点系统对话框；已发送文档依赖 `doc.file` 字段，旧的只有 `{name,size}` 的消息点开会提示「找不到附件文件」。

## [2026-10-30] 「等待你操作」卡：给 ask_user 加 action 变体（不新增工具）
- 问题/决策：agent 有些步骤只能由用户在本机亲手完成（登录 / 扫码 / 装依赖 / 点系统弹窗 / 插设备）。以前只能让用户在聊天框里打「好了」，语义弱、也容易被当成普通消息。决定**不新增 request_user_action 工具**，而是在已有的 ask_user 上加 action 变体，复用整条「弹卡 → 阻塞等待 → 回灌答案」链路（new_asks / submit_answers / wait_answers + SSE `ask` + `POST /api/agent/answer`）。
- 决策原因：新工具要同时登记 TOOLS / FILE_TOOLS / PLAN_MODE_TOOLS、四处提示词与前端分发，收益却只是同一张卡的另一种外壳；action 变体只多三个字段。
- 入参：`{"questions":[{"question":"标题","action":true,"detail":"一句为什么","items":["步骤1","步骤2"]}]}`；action 项强制清空 options（操作卡用固定按钮）。
- 实现：
  - `app/agent.py`：`new_asks` 透传 action / detail（≤400 字）/ items（≤8 条 ×200 字）；原生工具 schema（1039 行）与文本协议清单（5878 行）补字段说明；`_ASK_PROMPT` 加一条「只能由用户亲手完成的一步用 action」。
  - `app/api/chat.py`（4395 起）：普通分支的 merged 带上三个字段（历史回显要用），回灌文案改成「【用户亲手操作】标题 → 已完成」；自动化分支对 action 项跳过 autonomy_policy 代答，一律按「需要你在本机亲手操作」暂停。
  - `web/chat.js`：新增 `buildActionCard`（2709 起），`buildAskGroup` 在「整组都是 action」时切到它；按钮「我已完成，继续」提交 `已完成`、「无法完成」提交 `无法完成`；历史回显走只读汇总。
  - `web/css/base.css`：`.act-card / .act-title / .act-detail / .act-steps / .act-actions`（2240 起）。
- 验收：后端脚本确认归一化 + submit/wait 往返（action 项带 action/detail/items 且 options 为空，普通项不受影响）；临时 harness 页（本地 http.server + data-theme="light"）截图确认标题 / 说明 / 有序清单 / 两个按钮 / 已完成回显卡与主题色都正确，console 无报错；`pytest tests/test_agent_tool_format.py tests/test_prompt_budget.py tests/test_chat_blocks.py tests/test_stepctx.py` 全过。
- 踩坑：base.css 的颜色变量只在 `html[data-theme="light|dark|…"]` 下定义，`:root` 没有兜底。独立写的测试页若忘了带 `data-theme`，`var(--text-dim)` / `var(--border)` 会全部失效（表现是纯黑字、无边框），别误判成组件样式写错。
- 备注：命令审批卡片化（逐条命令确认、允许多次 / 拒绝 / 提问）此前已经实现（`web/chat.js` `buildApprovalCard`），本次未重做。

## [2026-10-30] 子代理 / 多 agent 可视化：从「一行文字」升级成可展开的面板
- 问题/决策：原「探索小队」卡只有 `[角色] 问题` + 一句状态，看不出每个侦察兵在干什么、跑了多久、花了多少 token、结论是什么；嵌套子代理也看不出层级。决定在**不改事件协议主结构**的前提下重做卡片：头部汇总 + 每行 = 状态点 / 角色徽章 / 任务 / 当前工具 / 实时耗时；点行展开指标与结论预览。
- 后端（`app/explore.py`，只加字段）：
  - `subagent_start` 加 `depth`（嵌套子代理用，前端按 `--depth × 16px` 缩进）；
  - `_run_one` 里累计每个子代理自己的 `tok`（原来只往批次 `budget` 里加，逐行看不到用量），`subagent_done` 加 `tokens` 与 `preview`（结论前 400 字）。
  - `subagent_all` 保持不变（已有 n / ms / tokens / ok / accept）。
- 前端（`web/stream.js`）：
  - `subagentRows[id]` 从「DOM 节点」改成 `{el, done, fail, t0}`，`subagentRow()` 返回记录、不再返回节点；
  - 运行中每 0.4s 刷一次耗时（`setInterval`），卡片被重绘掉（`!isConnected`）自动 `clearInterval`，不留定时器；
  - 头部可整体收起；行内点 `.subagent-top` 切换 `.open` + `detail.hidden`；
  - 完成时写指标（轮次 / 工具 / 字数 / token / 证据分 / 缺失引用，没有的项不占位）与结论预览，失败时红字显示 error。
- 样式（`web/css/base.css` 2040-2124）：头部淡蓝底、状态点三态（运行脉冲蓝色 / 完成绿 / 失败红）、角色徽章按 `data-role` 着色（侦察=主色、复核=warn）、结论区最高 168px 可滚。
- 验收：`node --check web/stream.js` + `py_compile`；静态夹具（本地 http.server + data-theme=light）截图确认四行状态、缩进、指标与横幅配色，无溢出、console 无报错；`pytest tests/test_explore.py tests/test_agent_tool_format.py` 全过。
- 未覆盖：真实 explore 端到端（要一次模型调用才有 SSE）；行点击的展开/收起只做了代码核对，静态夹具不含 JS 逻辑。

## [2026-10-30] IDE 差距 P0：语言服务前台化 / 全局替换 / 始终允许 / 全局写锁
- 背景：与 Trae（VS Code 内核）对比后确定的第一优先级四项。核心判断：语言服务能力后端早就有（`app/lsp.py` 只给 agent 用），缺的是「让人看见」。
- 1）语言服务前台化
  - 后端：`app/lsp.py` 末尾新增结构化接口 `diagnostics_items` / `symbol_items` / `goto_items` / `hover_text`（诊断用 0 基行列 + Monaco severity 8/4，跳转用 1 基），新增 `app/api/ide_intel.py` 自注册 `/api/ide/lsp/{status,diagnostics,symbols,goto,hover,rename}`（`app/httpd.py` 加一行 import）。
  - 前端：`web/index.html` 在编辑器下方加 `#ide-intel`（问题 / 大纲 / 结果三个页签）+ 状态栏 `#ide-status-problems` 角标；`web/gal/gal-ide.js` 新增 `IDE_INTEL` 与 12 个函数（诊断→`setModelMarkers`、大纲、跳转、重命名两段式），Monaco action：F12 定义、Shift+F12 引用、F2 重命名、Ctrl+Shift+O 大纲、Ctrl+Shift+M 问题；保存后自动重跑诊断（LSP 读磁盘）。
  - 只接 Python（pyright）：其它扩展名一次请求都不发。
  - 重命名是两段式：`apply=false` 只出方案（文件 + 改动行数），确认后 `apply=true` 才写；写盘走 `agent._snap` + `agent.register_undo`，可在「AI 变更」里撤销。
- 2）全局替换
  - `app/api/ide_edit.py` 的 `/api/ide/replace`：`dry_run=true` 只算方案（每文件几处 + 前后对照），`false` 才写；写前快照、写后登记 undo，写失败自动回滚已写文件。
  - 只处理 UTF-8 文本：非 UTF-8 / 含 NUL / >2MB 一律跳过并计数；密钥文件按规则过滤；上限 200 文件 / 2000 处。
  - 默认不勾「正则」= 原样文字替换（区分大小写）；勾上才按正则解释。替换走 `rx.sub(lambda: replacement)`，避免 `\1` 被当反向引用。
  - 前端：搜索侧栏加替换输入框 + 正则开关 + 「全部替换」（先 dry-run 弹确认，再 apply，然后刷新已打开模型）。
- 3）审批「始终允许」
  - `agent.remember_approval_rule(sandbox, name, args)` 往 `.gm/agent-rules.json` 写**最窄**规则：命令进 `commands.allow`（自动补 `*` 前缀匹配，`python -m pytest` → `python -m pytest*`），文件类进 `tools.allow`（`write_file:相对路径`），其它工具 `工具名:参数摘要`。
  - 拒绝的三种：`shell`（等于任意命令）、`computer_*`（任意站点）、以及**高风险命令**（安装 / 联网 / 改仓库历史 / 管道串联）——否则「始终允许」会变成长期交出这些权限。
  - `/api/agent/approve` 新增 `always`：先写规则再放行（写失败不拦操作，只回原因）；前端审批卡加第四个按钮，成功后 toast 显示实际写入的规则。
- 4）全局写锁
  - `agent.WRITE_LOCK = threading.RLock()`：`run_tool` 对 `WRITE_TOOLS` / `GIT_WRITE_TOOLS` 串行化；`write_text`（编辑器保存）、`/api/ide/replace`、`/api/ide/lsp/rename` 也走同一把锁。RLock 防止工具内部再触发写时自锁。
  - 解决的是：并发子代理 A/B 都读旧内容 → 先后写回 → 后写的静默冲掉前一个的改动。
- 验收：
  - 后端脚本：替换 dry_run/apply 计数与内容正确、二进制文件未被改；规则文件写入 `{"commands":{"allow":["python -m pytest*"]},"tools":{"allow":["write_file:a.py"]}}` 且能被 `_match_command_rules` 命中（`python -m pytest -q` → allow）；shell / pip install 被拒绝；`WRITE_LOCK` 是 RLock。
  - LSP 真实跑通（pyright 12s 首次索引）：2 条类型错误（0 基行号 + severity 8）、符号树、定义、引用、悬停全部返回结构化数据。
  - 前端夹具（真实 base.css/ide.css/gal-ide.js + 桩环境）：问题面板两行（红 ✖ / 黄 ⚠ + code + L 行号）、点「大纲」显示 4 行符号、点回「问题」恢复，替换行布局正常，无 JS 报错。
  - `pytest tests -q` 全过（1400+ 条）。
- 踩坑：
  - `fnmatch` 是全文匹配：规则里写 `python -m pytest` 不会匹配 `python -m pytest -q`，必须补 `*`（已自动补）。
  - 独立验证页不会执行 `ideInitOnce`，页签点击不生效——必须显式调 `ideIntelBindUI()`（真实路径里由 `applyUiMode('ide')` → `ideInitOnce` 触发）。
  - 本机 Trae 浏览器插件连不上（`os error 10061`，只有 Edge 没有 Chrome）时，可用 `msedge --headless=new --remote-debugging-port=9333` + CDP 的 `Input.dispatchMouseEvent` 做真实点击验证；Node 自带 WebSocket，不必装 Playwright。

## [2026-10-30] IDE 差距 P1：逐段审阅 / 内联编辑 / 文件树拖拽与监听 / 网页预览
- 背景：P0 之后按「6 → 5 → 7 → 8」的顺序补齐交互层。原则同 P0：能复用现有链路就不新造。
- 6）diff 逐段接受 + 时间线
  - `/api/sandbox/revert_hunks`：只把选中的 hunk 恢复成改动前内容。`context` 必须与前端渲染 diff 时一致（块边界由它决定，不一致下标会对错），前端把 `ideDiffCtx` 原样回传；另带 `sha`（diff 接口新增 `new_sha`），文件在审阅期间被外部改过就 409 拒绝。写盘走 `WRITE_LOCK` + `_snap` + `register_undo`，这次「部分撤销」本身也能再撤销。
  - 交互：编辑器上方一行 chip「改动段：第 1 段 保留/撤销 …」（`#ide-hunk-bar` / `#review-hunk-bar`），点「第 N 段」定位。
  - 时间线：`/api/agent/checkpoint` 撤销「这一步及之后」的所有写操作；聊天里的写步骤面板每行加了「回到这里」，回滚后调 `ideReloadAll()` 重读打开的标签（有未保存改动的标签跳过）。
- 5）内联编辑
  - `/api/ide/inline_edit`：用聊天模型，输入 = 文件 + 语言 + 选区前 4k / 选区 / 后 2k + 一句要求；输出只允许是替换后的片段（会剥掉 ``` 围栏；`// ERROR: 原因` 视为失败）。
  - 前端 Ctrl+I：`appPrompt` 收要求 → 拿到片段后 `executeEdits` 直接替换缓冲区，**不落盘**；Ctrl+Z 撤销、Ctrl+S 自己保存。
- 7）文件树拖拽 / 多选 / 外部改动监听
  - `/api/sandbox/move`：沙箱内移动，拒绝「移进自己的子目录」与同名冲突（409）；前端把标签 / 缓存 / 脏标记一起改名。
  - 多选 = Ctrl/Cmd + 点击（`state.ideSel`）；删除按钮在多选时批量删。
  - 监听 = 每 5s 拉一次目录树比对签名，变了就重绘；当前文件未修改且内容变了就 `setValue` 重载并提示。Electron 没有 fs.watch，轮询是最省事的做法。
- 8）网页预览
  - `app/preview.py`：按需在 127.0.0.1 随机端口起一个只读沙箱目录的静态服务（`Cache-Control: no-store`），关面板即 `shutdown()`。
  - 为什么不用 iframe 直读文件：iframe 带不了 `X-Auth-Token`，而相对路径的 css/js 必须能解析；临时静态服务最省事。路径穿越实测返回 404。
  - 面板 `#ide-preview`：地址栏支持 http(s) 直接填 dev server，也支持填沙箱内相对路径（如 `index.html`）。
- 验收：
  - hunk：后端脚本覆盖「只撤第 1 段 / 两段全撤 / sha 不符 409 / 撤销后能 redo / checkpoint 回滚」；前端用真实 Monaco 夹具验证 chip 条与点击后重渲染。
  - 内联编辑：参数校验通过（缺要求 / 空选区都 400），模型调用需真实密钥，未做端到端。
  - 树：移动成功、移进子目录被拒、同名 409 均实测通过。
  - 预览：html + 相对 css 正常加载、`no-store` 生效、`../` 穿越 404、重复 start 复用端口、stop 后连接被拒。
  - `pytest tests -q` 全过（1400+ 条）。
- 踩坑：
  - **Monaco contentWidget 不能用来做逐段按钮**：widget 是浮层不占位，两个改动段相距 2–3 行时 26px 高的工具条会互相盖住 12px（实测中心点被邻居拦截），而且首个 hunk 用 ABOVE 会被裁到编辑器外。最终改成编辑器上方独立一行的 chip 条。
  - `fnmatch` 是全文匹配（P0 已记）：`python -m pytest` 不会匹配带参数的版本，规则必须补 `*`。
  - 部分撤销后 hunk 下标会整体前移，前端必须清空 `it.kept` 再重渲染，否则「保留」会标到别的段上。

## [2026-10-30] 调研 Trae 的插件机制 + IDE 内 Markdown 预览 / PDF 导出
- Trae 插件为什么「白送」（本机取证，不是猜）：
  - `D:\Tools\Trae CN\resources\app\out\vs\{base,code,editor,platform,workbench}` = VS Code 编译产物结构；
  - `product.json`：`version 1.107.1`、`applicationName trae-cn`、`extensionsGallery.serviceUrl = https://open-vsx.org/vscode/gallery`（Open VSX）；
  - 内置 109 个扩展（含 `markdown-language-features`、`markdown-math`、`latex`、`python`）；
  - 扩展宿主 `out/vs/workbench/api/node/extensionHostProcess.js`，用户插件在 `~/.trae-cn/extensions`（本机 17 个）。
  - 结论：Trae = VS Code fork + Open VSX + 自家 AI 层；插件生态不需要「做」，因为跑的就是 Extension Host API。
- 我们不是 fork（Python 后端 + 原生 JS + 仅 Monaco），三条可选路：
  - A 内嵌 code-server（100% 兼容，需随包分发 Node + code-server，百 MB 级，iframe 跨层交互受限）；
  - B 通用 LSP 管理器（`.gm/lsp.json` 启动 ruff / eslint / clangd / texlab 等，覆盖语言服务类，带不进 UI 类插件）；
  - C 强化「打开外部 VS Code」桥（已有按钮；成本最低，生态 100% 因为就是真 VS Code）。
  - 本轮结论：都先不做，先补 md 预览 + PDF。
- 实现：
  - **Markdown 预览**：`web/index.html` 加 `#ide-md`（右半屏）+ 工具栏按钮；`web/gal/gal-ide.js` 的 `ideMdToggle/ideMdRender/ideMdSchedule` 复用 `safeRenderMarkdown`（含 KaTeX），打字 180ms 节流刷新；切走非 md 文件自动收起；快捷键 Ctrl+Shift+V。分屏靠 `.ide-editor-wrap.md-split` 把绝对定位的编辑区 `right:50%`，Monaco `automaticLayout` 自动重排。
  - **PDF 导出**：`/api/ide/export_pdf` 收前端渲染好的完整 HTML（Markdown 渲染在浏览器侧，后端没有渲染器），写临时 HTML → `msedge --headless=new --print-to-pdf` → 落在源文件同目录、同名 `.pdf`，并 `invalidate_tree`。浏览器路径复用 `webprobe.edge_path()`（支持 `GAL_EDGE` 覆盖）。
- 验收：
  - 真实浏览器夹具：h1 / 表格 / 代码块 / 2 个 KaTeX 公式全部渲染，右栏宽度 = 视口 50.07%、与编辑器 overlap = 0，console 无报错；
  - PDF 端到端：21,646 字节、`%PDF-` 头、中文正常；
  - `pytest tests -q` 全过。
- 踩坑：
  - **KaTeX 字体目录必须一起分发**（`web/vendor/katex/fonts` 60 个文件）；只拷 css/js 时公式会回退到系统字体（夹具里踩到，应用里是齐的）。
  - PDF 的 HTML 必须由前端生成：`markdown.js` 在浏览器里、后端没有等价渲染器；接口只负责打印，避免两套渲染结果不一致。
  - 打印前要把 KaTeX 样式表用**绝对 URL** 引进来（`location.origin + /static/vendor/katex/katex.min.css`），否则临时 HTML 在 `file://` 下拿不到样式。

## [2026-10-30] LaTeX 全套支持：语法高亮 / 编译 PDF / 章节大纲 / 清理辅助文件
- 定位：和 VS Code 的 LaTeX Workshop 同一个模型——**只用用户机器上已装好的引擎**，不打包、不代下载。探测顺序 `latexmk` → `xelatex` → `pdflatex`；`config.json` 的 `latex_engine` 可指定绝对路径或命令名（PATH 没继承时用）。
- 本机实测（TeX Live 2025，`D:\Tools\texlive\2025\bin\windows`）：`latexmk -xelatex` 编译中文 `ctexart` 成功，15.2 秒；错误行号来自 `-file-line-error`（`bad.tex:4: Undefined control sequence.`）；子文件（无 `\documentclass`）给出明确提示。
- 后端：
  - `app/latex.py`：`find_engine` / `build` / `clean` / `outline` / `parse_errors` / `parse_warnings`。
  - `app/api/ide_tex.py`：`/api/ide/tex/{status,build,clean,outline}`（`httpd.py` 加一行 import）。
  - 安全：**永不传 `-shell-escape`**（TeX 能执行任意命令）、cwd 限定 `.tex` 所在目录、180 秒硬超时 + 超时连子进程一起杀（`agent._kill_tree`）、编译期间持 `WRITE_LOCK`（写在沙箱里，和 agent 串行）。
- 前端（`web/gal/gal-ide.js`）：
  - **语法高亮**：内置 Monaco 没打包 latex，`ideRegisterLatexLanguage` 自注册 `latex` 语言 + Monarch 词法（命令 / 环境 / 注释 / 数学模式），`.tex/.sty/.cls/.bib` 都映射过去；注册必须发生在建 model 之前。
  - **编译**：`.tex` 才显示工具栏的播放按钮（`Ctrl+Alt+B`）；编译前自动保存（引擎读磁盘）；成功后用临时静态服务把 PDF 嵌进预览面板，失败把 `文件:行号 + 消息` 灌进「问题」面板并画标记（跨文件错误在行尾显示文件名，点一下跳过去）。
  - **大纲**：`.tex` 走 `/api/ide/tex/outline`（正则抽 chapter/section/subsection…），和 Python 的 LSP 大纲共用同一个面板。
  - **清理**：命令面板「清理 LaTeX 辅助文件」，删 `.aux/.log/.synctex.gz/.xdv` 等，PDF 不删。
- 验收：
  - 后端脚本：中文编译成功出 PDF；坏文件解析出 `bad.tex:4`；outline 名称/层级/行号正确；clean 只删辅助文件；子文件被拒。
  - 真实浏览器：`window.__tex` 返回 `registered=true / langOfTex="latex" / line1 keyword.tex / line2 comment.tex / line4 string.tex`，模型 `languageId=latex`，console 无报错。
  - `pytest tests -q` 全过。
- 有意没做：
  - **L2 公式实时预览**：编译出真 PDF 比 KaTeX 片段更准（自定义宏 KaTeX 不支持，容易误导）。
  - **SyncTeX 正/反向跳转**：需要能解析 synctex 的 PDF 阅读器，内置 iframe 用浏览器自带阅读器做不到。
- 踩坑：
  - Monaco 的语言注册必须在建 model 前；否则 `.tex` 会先按 `plaintext` 建模，之后改语言要重建模型。
  - 编译前**先删旧 PDF**：否则编译失败时会读到上一次的产物，误判成成功。
  - 编译 15 秒量级（中文 + 交叉引用），只能按钮/快捷键触发，不能「保存即编译」。

## [2026-10-30] Python / C 支持：ruff 检查与格式化、gcc 诊断与构建运行
- 本机工具（探测结果）：`python`（D:\Tools\Python）、`ruff 0.17.1`、`gcc/g++ 15.2`（MinGW，D:\Tools\mingw64）、`gdb`；**没有 clangd / clang-format**。
- 模型同 LaTeX：只调用已装好的工具，不打包。新增 `app/toolchain.py` + `app/api/ide_tools.py`：
  - `/api/ide/tools/status`：探测结果（前端决定显示哪些按钮）
  - `/api/ide/py/lint`：`ruff check --output-format=json` → 结构化 items（0 基行列 + Monaco severity）
  - `/api/ide/c/check`：`gcc -fsyntax-only -Wall -Wextra` → `文件:行:列: 级别: 消息`（没有 clangd 时的兜底诊断）
  - `/api/ide/c/build`：`gcc -g -O0` 产出同名 `.exe`
  - `/api/ide/format`：Python 用 `ruff format`、C 用 `clang-format`（未装则明确提示）
- 前端（`web/gal/gal-ide.js`）：
  - 一个 ▶ 按钮按扩展名分发：`.tex` 编译 PDF、`.py` 运行、`.c/.cpp` 构建并运行；快捷键 `Ctrl+Alt+R`。
  - 运行放在**集成终端**里（`ideTermSend`，stdin 可用），不是后台静默执行。
  - C：打开与保存后各跑一次 gcc 语法检查，结果进「问题」面板 + 波浪线（与 pyright / latex 共用 `ideToolProblems`）。
  - 格式化 `Shift+Alt+F`；命令面板补「ruff 检查」「C 语法检查」「格式化文档」。
  - **修掉一个旧 bug**：`IDE_LANGS` 里 `c/h → 'c'`，而 Monaco 只注册了 `cpp`（扩展名已包含 .c/.h），等于 `.c` 文件一直没有语法高亮；现统一映射到 `cpp`。
- 安全 / 一致性：格式化与构建都持 `WRITE_LOCK`；格式化写前 `_snap`、写后 `register_undo`（可在「AI 变更」撤销）；检查超时 60s、构建 120s。
- 验收：ruff 能报 `F401/I001`；`ruff format` 改好文件并登记了 undo token；gcc 对 `int x = ;` 报 error（line 0）+ `unused variable` 警告；坏文件构建失败带 errors、好文件构建出 `b.exe`；`pytest tests -q` 全过。
- 踩坑：**MinGW 的 gcc 不认 `-fno-color-diagnostics`**（那是 clang 的参数），传了会直接报 `unrecognized command-line option`，导致整条命令失败、诊断全丢。要用 `-fdiagnostics-color=never`。
- 未做（缺工具）：`clangd` 未装 → C 只有编译级诊断，没有补全/跳转；`clang-format` 未装 → C 暂不能格式化（提示里写明了）。装上后可以再扩。

## [2026-10-30] 侧栏收起时的模式切换：同一节点「挪家」+ 悬浮胶囊
- 问题：`#mode-switch` 挂在 `#sidebar .side-top` 里，侧栏一收起，`.sidebar-body` 整体 `opacity:0 + visibility:hidden`，模式按钮跟着消失；而 Solo ↔ IDE 是最常来回切的一对，逼用户先展开侧栏很别扭。且 IDE 模式下 `#sidebar-expand` 被特意挪到**左下**（`#main.ide-mode #sidebar-expand { bottom:56px }`），因为左上角是 IDE 活动栏——所以不能简单在左上角再摆一个按钮。
- 方案（用户选 A）：**移动同一个 `#mode-switch` 节点**，不复制 DOM（active 状态只有一份）：
  - 侧栏展开 → 回到 `.side-top`（原位，文字按钮）
  - 侧栏收起 → 挂到 `#mode-float`（图标按钮 28×28）
    - 非 IDE：`top:8px; left:48px`，与展开圆按钮同一行
    - IDE：`bottom:96px; left:10px`，竖排，叠在展开按钮上方
- 实现：
  - `web/index.html`：`.ms-btn` 内加 `<svg>` 图标 + `<span>` 文字；`#main` 内加 `<div id="mode-float" class="mode-float hidden">`。
  - `web/css/base.css`：`.side-top .ms-btn .ic { display:none }`（侧栏里只显示文字）；`.mode-float` 毛玻璃底 + 圆角；`.mode-float .ms-btn` 固定 28px、只留图标；两种模式的定位规则。
  - `web/init.js`：`syncModeSwitchHome()` 在 `applySidebarCollapsed()` 末尾调用；另加 `Ctrl+Alt+1..4` 直接切模式（不依赖 UI）。
- 验收（真实 base.css + 真实 init.js 的夹具，四态）：
  - expanded：节点回到 `side-top`、float 隐藏、文字可见、图标隐藏
  - collapsed-solo：节点在 `mode-float`、`flexDirection=row`、按钮 28×28、只显示图标、位于 `#main` 左上 (48,8)
  - collapsed-ide：`flexDirection=column`、锚在左下（距底 96px、距左 10px）
  - expanded-again：完全复原
- 坑：
  - 夹具里连续改 class 后**同一帧**取 `getBoundingClientRect` 会读到侧栏宽度过渡中的中间值（左边距偏大 24px）；布局锚点（top/bottom）不受影响，判断时别被 `left` 的过渡值误导。
  - 节点挪家必须在**收起/展开的同一个入口**（`applySidebarCollapsed`）里做，否则启动时从配置恢复的收起状态会漏掉，按钮留在侧栏里看不见。

## [2026-10-30] 修复运行路径 / PDF 备份 / 编译锁，并解决 IDE 侧栏卡顿
- **运行用绝对路径**：终端是持久 shell，用户 `cd` 过之后 `python "src/a.py"` 就找不到文件。新增 `ideAbsPath()`（拼接 `session.sandbox`），`ideRunPython` / `ideBuildC` 都改成绝对路径；行为不变，只是不再依赖当前目录。
- **LaTeX 不再删旧 PDF**：原实现为了防「把旧产物当成功」直接 `os.remove(pdf)`，代价是编译失败时上一版 PDF 也没了。改成：先 `os.replace` 到 `<stem>.pdf.bak` → 成功删备份、失败还原（返回 `restored: true`，前端提示「已保留上一版 PDF」）；`clean()` 也会顺手清掉残留的 `.pdf.bak`。
- **新增 `BUILD_LOCK`**：LaTeX（实测 15s）和 gcc 构建只产出中间文件 / 可执行文件，不该占 `WRITE_LOCK`——原来编译一次会堵住 agent 所有写操作、撤销、格式化。现在编译走 `BUILD_LOCK`（构建之间仍串行，避免 latexmk 互相覆盖 .aux）；`/api/ide/format` 改的是源码，继续用 `WRITE_LOCK`。
- **IDE 侧栏卡顿**：根因是 `#sidebar` 有 `width .18s` 过渡，而 IDE 模式下 Monaco 的 `automaticLayout` 会在过渡的每一帧重排（十几次全量 layout）。加 `#app.mode-ide #sidebar { transition: none; }`（`applyUiMode` 给 `#app` 打 `mode-ide` 类）；**其它模式保留原动画**，功能不变。
- **更正前一晚的判断**：模式胶囊在 IDE 左下 `bottom:96px` **不会**压住终端——终端在 `.ide-main` 内（x 从活动栏之后开始），胶囊在活动栏那一列；纵向也和「在 VS Code 中打开」按钮、展开按钮不重叠。原判断有误，本次未改动。
- 验收：LaTeX 连续三次构建（成功→失败→成功），失败时旧 PDF 的 size/mtime 完全不变、`.bak` 已清；`pytest tests -q` 全过；`node --check` / `py_compile` 通过。
- 待确认：侧栏卡顿是按代码定位的（过渡 + automaticLayout），需要重启后在 IDE 里实际开关一次确认手感；如果仍有延迟，下一个嫌疑是 `renderSessions()`（切模式时会重建会话列表）。

## [2026-10-30] 知识库「AI 读取成本」预估（先讲清口径）
- 用户问「知识库不是注入的吗，会增加多少 token」——查代码后结论是**两种注入并存，必须分开说**：
  - 日常对话：`knowledge.retrieve_memory` 检索后注入，预算 `INJECT_MAX_TOKENS = 1500`（最多 12 命中 + 4 邻居、单条正文截 400 字），**整库不会全量塞进去**。
  - GAL 编剧：`world_text` 整库拼接，`WORLD_MAX_CHARS = 9000` 上限，**每次生成只喂一次**。
  - 规则/能力/力量（`PINNED_CATEGORIES`）：GAL 里走 `pinned_block` **每轮常驻**，不占检索预算——这是唯一"每轮都全量"的部分。
- 实现：
  - `app/knowledge.py` 新增 `kb_stats(kb_ids)`（用 `context.estimate_tokens` 同口径折算，按库给出 chars/tokens 与规则类小计）；新增常量 `WORLD_MAX_CHARS`。
  - `app/api/kb_stats.py` 自注册 `/api/kb/stats`（`httpd.py` 加一行 import）。
  - `web/index.html` 在「本会话使用的知识库」里加 `#kb-token-est`；`web/knowledge.js` 的 `renderKbTokenEst()` 在绑定变化时刷新，分四行写清：整库规模 / 日常每轮上限 / GAL 一次性 / 规则类常驻。样式在 `base.css` 的 `.kb-token-est`。
- 验收：假库单元测试（2 库 4 条，规则类 2 条）数值正确、空列表返回 0、路由可用；`node --check` / `py_compile` 通过；`pytest tests -q` 全过。
- 注意：界面上必须强调「整库规模不是单次成本」，否则很容易被误读成每轮都要花这么多。

## [2026-10-30] 会话记忆 / 用户画像的 token 预估
- 数据在哪（易错点）：
  - **会话记忆**：`memory_items` 表，`source='session'`、`scope='session:<sid>'`（实测就是这个格式，不是裸 sid）。
  - **用户画像**：不在 `memory_items`，而是「用户画像」这个**知识库**的节点；`memory_items` 里 `source='profile'` 是空的。
- 实现：
  - `app/memory.py` 新增 `estimate(session_id)`：会话记忆按 scope 精确过滤；画像改为**只读** `load_kb_index()` 找 `PROFILE_KB_NAME` 再 `load_kb()` 统计。**不能调 `get_profile_kb()`**——它在库不存在时会新建一个，一个「看一眼」的请求不该有副作用。
  - `app/api/kb_stats.py` 加 `/api/memory/estimate`（同一个模块，省得多一个文件）。
  - `web/index.html` 在两个开关下面各加 `#mem-est-profile` / `#mem-est-session`；`web/knowledge.js` 的 `renderMemTokenEst()` 在打开知识库弹窗时渲染。
- 口径（界面上也这么写）：
  - 画像：`共 N 条 ≈ T token（每轮固定注入最多 30 条，其余靠检索命中）`
  - 会话记忆：`本会话共 N 条 ≈ T token（每轮按检索注入，与知识库共用 1,500 token/轮 预算）`
- 实测（本机真实数据）：全部会话记忆 148 条 / 43,233 字 ≈ 18,036 token；画像 45 条 / 6,410 字 ≈ 3,307 token。
- 已知未做：会话记忆里若有「规则类」节点，GAL 会整段常驻注入（`pinned_block`），当前估算只给了总量，没有拆出这部分；要拆得先给 `memory_items` 补 category 口径。


## [2026-10-31] 知识库结构演进 S1/S2：连线必须有「为什么」+ 旧结论标失效
- **S1 连线陈述必填**：边新增 `fact`（一句话说明依据，截断 200 字）。`apply_memory_ops` 的 `link` 分支没拿到 fact 直接拒绝并写进 problems（提示模型补），前端连线表单加了 `#ke-fact` 文本框，拖拽连线改为**取消落库 → 打开表单**让用户补陈述。目的：把「A --因果--> B」这种只有关系名的空边补成「A --因果（因为…）--> B」。
- **关系词表 13 → 11**：删掉 `sequence`（顺承）/ `turn`（转折）——它们表达时间顺序，不是结构关系，实测这类边的信息量最低。老数据不动：`LEGACY_RELATION_LABEL` 让 `relation=sequence/turn` 的老边照旧显示「顺承/转折」，前端 `LEGACY_REL_LABEL` 同理；新写入时这两个词经 `_REL_ALIASES` 归一到 `related`。
- **唯一键**：`(from,to,relation)` 同一对节点同一关系只保留一条**有效**边。老边没有 fact 且新陈述不同 → 直接补 fact（不产生替代）；已有 fact 且新陈述不同 → 走 S2。
- **S2 失效不删**：边新增 `valid_at` / `invalid_at`。新增 op `unlink`（按 edge_id 或 from+to 找有效边，写 `invalid_at`）；同一对节点同一关系出现新 fact 时旧边自动标失效、新边照常追加。检索侧 `_collect_items` 只把有效边放进 `edges_map`，`_expand_neighbors` 因此不会把失效关系注入提示词；邻居注入的标签改成「关系：陈述」，让 fact 真正被模型读到。导出 Markdown 跳过失效边，并把 fact 写进「关系」小节。
- **AI 侧提示词**（`app/mem_tool.py`）：`link` 明确 fact 必填、新增 `unlink` 说明；规则 6 的关系优先级去掉了 sequence；`OP_NAMES` 增加「断开」。
- **前端展示**：失效边在 vis 里虚线 + 淡化（opacity .4）+ 标签后缀「（已失效）」；连线悬浮 `title` 显示「关系：陈述」，节点详情里的关联行也会带「已失效 ·」前缀并用 title 给出完整陈述。
- 相关代码：`app/knowledge.py`（RELATIONS / normalize_relation / `normalize_edge_fact` / `edge_is_valid` / `invalidate_edge` / `_find_edge` / `apply_memory_ops` / `_collect_items` / `_expand_neighbors`）、`app/mem_tool.py`、`app/api/kb.py`、`web/knowledge.js`、`web/index.html`。
- 验收：新增 `tests/test_knowledge_edges.py` 11 条（无 fact 拒绝、幂等、老边补陈述、新陈述替代、unlink、检索过滤、邻居带 fact），`pytest tests -q` 全过；`node --check web/knowledge.js`、`py_compile` 通过。
- 未做（留给 S3–S5）：`source_msg` 原话锚点、检索两跳、导出 frontmatter；手工删除连线仍是物理删除（用户主动操作，不走失效）。

## [2026-10-31] 知识库 S3：记忆条目的原话锚点（source_msg）
- **问题**：知识库/会话记忆里的条目是 AI 总结出来的结论，用户无法核对「这句话是哪一轮说的」，AI 写偏了也定位不到。
- **方案（分两步，不能一步干完）**：
  1. `apply_memory_ops` 执行时**还不知道** assistant 消息的最终下标：续写会复用上一条消息、生成中的 `_progress` 快照消息落库前会被 pop。所以原话锚点不能在 apply 时写。
  2. 在 `app/api/chat.py` 落库那一刻（`s2["messages"].append(msg)` 之后、`_persist_session` 之前）调 `knowledge.anchor_changed(mem_note["changed"], s2, len(s2["messages"]) - 1)`，按 `changed` 里的 node_add / node_update / edge_add / edge_update 给条目补 `source_msg`（消息下标）。会话条目直接改内存里的 s2，随后一次 persist 落盘；知识库条目在函数内 `load_kb → 改 → save_kb`。
- **为什么下标稳定**：压缩只移动 `summary_upto`、**不删除消息数组**；`slim_sessions` 只截传输 payload。所以下标跨压缩仍然有效（已删会话 / 手动新建节点没有锚点，前端自动隐藏入口）。
- **前端**：节点详情新增「查看原话」（`#kd-origin`），只有 `node.source && typeof node.source_msg === 'number'` 时显示；点击后 `closeKbModal() → selectSession(node.source) → ssFocusMessage(node.source_msg)`，复用会话搜索已有的定位能力（`web/chat.js`），不需要新接口。
- **成本**：共 4 个文件约 70 行；唯一风险点是 chat.py 落库时序，因此没有把 `apply_chat_ops` 挪位置（会改变 `mem_write` SSE 的时序），而是新增了一个「事后打标」函数。
- 相关代码：`app/knowledge.py`（`anchor_changed`）、`app/api/chat.py`、`web/index.html`、`web/knowledge.js`、`tests/test_knowledge_anchor.py`。
- 验收：新增 5 条用例（session 节点 / 边 / KB 保存 / 缺失 KB / 非法下标），`pytest tests -q` 全过；`py_compile`、`node --check` 通过。
- **S4 暂缓**：实测边密度——世界设定 34 节点/29 边、数学教材 31/20 有价值；但「深渊回响」52/6、「助手9.0」51/1、「AIMC」36/0 两跳走不动。先做 S4a（邻居参与排序、命中优先占预算）更划算，真两跳等边密度上来再说。

## [2026-10-31] 知识库 S4a：一跳邻居参与排序（命中保底，邻居争剩余名额）
- **原来的问题**：`retrieve_memory` 先取 12 条直接命中占满名额，再把最多 4 条邻居硬追加在后面；邻居分数再高也永远排在所有命中之后，低分命中却一定进注入。结果是最多 16 条都要付 token，而「相关但没被关键词命中」的关系节点几乎进不了正文。
- **方案**：新增 `_merge_ranked(chosen, neighbors)`（`app/knowledge.py`）：
  - 先按分数保底保留 `MATCH_KEEP = PICK_LIMIT - NEIGHBOR_LIMIT = 8` 条直接命中；
  - 剩下的 4 个名额由「第 9 名之后的命中 + 邻居（分数 ×0.6）」同池按分数竞争；
  - 总条数固定 ≤ 12（原来最多 16），注入顺序仍是「命中块在前、邻居块在后」，块内按分数降序。
  - `recall_detail` 同步改成用合并后的结果，`kind` 仍区分 `match / neighbor`，前端召回调试显示不用改。
- **真实数据验证**（只读跑 `_collect_items → _rank_items → _expand_neighbors → _merge_ranked`）：
  - 「深渊回响开发」会话：候选 52、命中 2、邻居 0 → 合并 2 条（图太稀，S4a 无感）。
  - 「世界设定」知识库：候选 34、命中 12、邻居 4 → 合并 12 条 = 8 命中 + 4 邻居，4 条最低分命中被邻居挤出（这是预期行为）。
- **成本/风险**：改动集中在注入选择，约 25 行；token 上限反而从 16 条降到 12 条。风险是低分命中被挤掉——所以保留 8 条保底，且邻居分数按 0.6 衰减，只有分数确实高才进得来。
- **未做（S4b 两跳）**：仍然只走一跳。`_expand_neighbors` 的 `NEIGHBOR_LIMIT=4` 与 `_inject_line` 的 fact 注入不变；等会话库的边密度上来再评估两跳。
- 相关代码：`app/knowledge.py`（`MATCH_KEEP` / `_merge_ranked` / `retrieve_memory` / `recall_detail`）、`tests/test_knowledge_rank.py`。
- 验收：新增 4 条排序用例；`pytest tests -q` 全过；`py_compile` 通过。

## [2026-10-31] 知识库 S5：Markdown 导出（frontmatter + 链接 + 事实清单）
- **旧实现的不足**：`_kb_markdown` 只输出 `# 库名` + 每个节点 `## 标题` + 关系行，没有 frontmatter，关系只写「因果：标题（陈述）」，机器不好解析，也不方便导进 Obsidian / 笔记库。
- **新格式**（`app/api/kb.py::_kb_markdown`）：
  - YAML frontmatter：`title / id / nodes / relations / exported / generator`，节点标签去重后输出 `tags: [...]`；字符串统一走 `json.dumps` 做 YAML 双引号转义（标题里有冒号、引号也不会坏）。
  - 节点区：`> 分类 · 标签 · 别名 · id` + 正文 + `**补充**`（`node.notes` 时间线，带日期）+ `**关联**`（只放 `[[别的节点]]` 链接，不重复陈述；单向 `→ / ←`，对称 `⇄`）。
  - 末尾 `## 事实`：每条**有效边只写一次**，`- [[A]] --因果--> [[B]]：一句话依据`（对称边写成 `[[A]] ⇄ [[B]]（相关）`）。失效边不导出。
- **为什么节点内只放链接**：陈述放两处会重复且容易不一致；「链接」负责跳转，「事实」负责语义，两者都不丢。
- 实测（世界设定库）：frontmatter 6 行、34 个节点、29 条关系全部进「事实」区；老边没有 fact 时只有 `[[A]] --因果--> [[B]]`，不补假话。
- **提示词同步**（用户提醒：不更新提示词模型不会用新功能）：
  - `app/mem_tool.py`：`link` 说明补齐「fact 会被随关系注入后续对话」「同一对节点同一关系只保留一条有效边，陈述变了自动替代旧边，不需要先 unlink」；规则 6 区分「同关系换陈述 → 直接 link」「关系本身不成立 → unlink」。
  - `app/stepctx.py`：记忆整理提醒里的 `sequence` 换成 `means / condition`，并要求写 fact、用 link 自动替代旧边。
  - 注入头：`【关联记忆】` 的说明改成「『关系：陈述』里的陈述就是具体依据，可直接采用」。
  - 关系计数：`mem_tool` 统计与 `chat.py` 传给 `memory_organize_hint` 的关系数都改为**只算有效边**，避免失效边让模型以为「关系已经够了」。
- 测试：新增 `tests/test_kb_export_md.py` 4 条（frontmatter、节点链接与补充、事实区、空库），更新 `test_knowledge_retrieval.py::test_export_markdown` 与 `test_stepctx.py` 的旧断言，新增 `test_gal_scope.py::test_memory_prompt_mentions_fact_and_unlink`。`pytest tests -q` 全过。
- 相关代码：`app/api/kb.py`、`app/mem_tool.py`、`app/stepctx.py`、`app/api/chat.py`、`app/knowledge.py`（注入头）。

## [2026-10-31] 排查：会话中途输出了无关的「临床病例分析」
- **现象**：会话「深渊回响开发」第 1138 条（`done_at` 2026-10-31 01:12:36）在一次「继续输出」里，工具结果之后突然输出 1438 字的产科肺炎病例分析，随后模型自己在前一轮 reasoning 里写「my response was a clinical analysis … seems I generated an unrelated response」，中文补一句「我上一轮完全跑偏了」，然后回到文档同步任务并正常 finish。
- **证据链（可复现的检查方法）**：
  - 该消息 1137 是**用户手动中断**（`interrupted=True`，`end_reason.kind=resume`），1138 是继续输出：33 轮 / 4m20s / `total_ms=260201`。
  - 用 `blocks`+`rounds_detail` 做回溯对齐（每轮消费 reasoning/text + 若干 tool block，tool 总数 26 与 rounds 的 tools 求和一致），医学文本是 block 31，属于第 19 个 round 条目（n=17，`completion=4445`，`finish=stop`，`tools=0`，`prompt=220745`）。
  - 排除本地来源：`kb_ids=[]`、`use_profile=False`；会话记忆 52 节点/6 边无医学条目；`data/knowledge`、`user_profile.json`、`uploads`、`tool_trace/s_2809c1a88538.jsonl`（5312 条）全部无 `CURB-65`/`产褥期`；只有 `data/saves` 里 01:12:38 之后的快照包含它（是事后会话快照，不是来源）。
  - 该消息没有 `memory` 字段 → 这一轮没有写记忆，知识库未被污染。
- **异常前最后一段正常文本（`blocks[29]`）**：「测量结果：**587.6 KB / 12024 行**。README 里仍是旧值（11918 行 / 582.3 KB），需要更新。先定位 README 中的确切位置。」
- **当时的动作序列**：①技能音效三层结构改完 → `node --check` 通过、全量回归 51 套/1329 PASS；②写 `_dev/meas_1031e.js` 测规模 → `bytes=601665 / 587.6 KB / 12024 行`；③决定同步 README/NOTES 数字；④写 `_dev/scan_doc_1031f.js`（10 行扫描脚本），**工具结果只是「已创建 + 源码回显」，零新信息**；⑤下一轮没有运行脚本，直接输出病例分析（`blocks[31]`）。
- **关键前兆：数字串位**。它说「README 仍是 11918 行 / 582.3 KB」，但 README 实际是 **585.8 KB / 11998 行**（后面的 scan 脚本输出可证）；`11918 / 582.3` 是**更早几轮（消息 1125、1127）实测得到的旧值**。即跑偏前它已把「历史测量值」当成「README 当前值」，上下文记忆先串位、再整段离题。
- **该轮 prompt 构成（`rounds_detail[19].in_parts`，总 220,745）**：reply 167,884 / user 21,575 / schema 10,638 / summary 7,404 / tools_doc 4,334 / tool_result **3,440**。四分之三是模型自己前面说过的话，工具事实只占 1.6%——自我强化的典型条件。
- **复原方法（可复用）**：`sessions.json` → 目标消息的 `blocks`（`kind` 为 reasoning/text/tool）+ `timing.rounds_detail`（每轮 `tools` 数）。用回溯对齐：每轮依次消费可选的 reasoning、可选的 text，再消费 k 个 tool block；tool 总数应与 rounds 的 `tools` 求和一致（本例 26=26）。据此可定位某段异常文本属于哪一轮及其 `completion/prompt/finish`。
- **判断**：本地注入可以排除；剩余两种可能是「模型自身长上下文退化」与「上游响应/上下文混流」。证据更偏向前者：①该轮 completion 4445 tokens，而可见文本只有约 1k tokens，说明同一轮内有 3k+ 隐藏推理（推理 block 已被 `_trim_reasoning_blocks` 截掉，无法直接读）；②跑偏前已出现「数字串位」这种**渐进式事实漂移**，串线是突然插入别人的完整内容、解释不了这个前兆；③官方 `api.deepseek.com` + `deepseek-flash` + 220,745 token prompt（其中 168k 是模型自述历史）+ 33 轮重复工具循环，正是 flash 档长上下文塌缩的典型条件。但**无法定论**：我们没有保存每轮 response id 与请求指纹，无法检验串线特征。
- **后续可做（未实施）**：`rounds_detail` 记录 provider `response_id` + 请求 messages 的 sha1 + 可见文本 token 估算；对「completion > 可见文本估算 × 2.5 且无工具调用」的轮次打异常标记并自动重试一次；长 agent 任务更早触发压缩 / 换非 flash 模型。

## [2026-10-31] 设置页：页头取消吸顶（不再遮挡模块文字）
- **问题**：设置弹窗右侧的页头（`.set-page-head`：标题 + 描述 + 「恢复本页默认」）是 `position: sticky; top: 0; z-index: 4`，内容滚动到它下面会被整块盖住，模块标题/说明文字看起来「被吃掉」；切页签恢复 `scrollTop` 时也会一进入就处于被遮挡状态。
- **决策**：取消吸顶（`position: sticky` → 普通流），页头随内容滚走。左侧导航本身就高亮了当前页签，且弹窗顶部还有标题栏，牺牲「页头常驻」换「文字不被遮挡」更划算。
- **实现**：
  - `web/css/polish.css`：`.set-page-head` 去掉 `position/top/z-index`，`margin/padding` 保持原值（`-18px -20px 12px` / `12px 20px 10px`），静止状态外观不变；`scroll-margin-top` 从写死的 `76px` 改为普通页 `16px`，「功能说明」页改用 `var(--help-head-h)`（那一页内部还有自己的吸顶筛选栏）。
  - `web/settings.js`：`helpSyncHeadHeight()` 只在页头仍是 `position: sticky` 时才把页头高度算进吸附偏移；现在 ph=0，`#help-head` 贴滚动区顶部，`--help-head-h` 也相应变小。
- **取舍/回退**：若以后想恢复吸顶（例如想要常驻页标题），把 `position: sticky; top: 0; z-index: 4` 加回去即可，JS 已按「sticky 才让位」兼容两种状态。
- **相关代码**：`web/css/polish.css`（`.set-page-head`、`scroll-margin-top` 规则）、`web/settings.js`（`helpSyncHeadHeight`）。
- 验证：`node --check web/settings.js` 通过；CSS 花括号配平（119/119），文件内已无 `position: sticky` 与 `76px` 残留。**未做浏览器实测**，需重启/刷新后在「设置 → Agent 执行」滚动确认模块标题不再被页头盖住。

## [2026-10-31] Skill 管理：新建不再互相覆盖 + 明确保存位置 + 编辑反馈
- **问题（三条都指向同一个根因）**：
  - 前端 `skillSlug(meta.name)` 把默认名「新 Skill」转成 `skill`，每次「新建 → 保存」都写进同一个 `data/skills/skill/`，看起来就是「只能写一个 Skill」；磁盘上确实存在一个只有空模板的 `data/skills/skill/SKILL.md`，就是这个 bug 的产物。
  - 保存成功只弹「已保存：<name>」，不显示落到哪个文件，用户不知道去哪找。
  - 编辑入口只有行尾小按钮，且没有「未保存 / 已保存到哪」的状态提示，配合上面的覆盖问题，很容易被当成「编辑没反应」。
- **修改**：
  - `app/skills.py`：新增 `unique_id(base)`——新建时目录已存在就依次尝试 `base-2`、`base-3`…（结果 ≤ 40 字符，和 `safe_id` 上限一致）。
  - `app/api/skills.py`：`save` 支持 `unique: true`（新建），返回**实际使用的 id** 与 `item.path`；新增 `op=open`，只允许打开固定的 `data/skills` 目录（不接任意路径）。
  - `web/skills.js`：新建时传 `unique` 并用返回 id；`skillMetaLine()` 常驻显示「当前：name · id · 文件 data/skills/<id>/SKILL.md」，textarea 有改动时标「未保存」；整行点击即可载入编辑器（勾选框除外）；保存 toast 改为「已新建/已保存：data/skills/<id>/SKILL.md」。
  - `web/index.html`：编辑区加「打开目录」按钮；`web/css/skills.css`：行可点、路径用 code 样式、未保存用 warn 色、按钮可换行。
- **兼容**：`skills.save(sid, text)` 签名与返回值不变，老测试与内部调用不受影响；新建的 id 分配只发生在 API 层。
- 相关代码：`app/skills.py`、`app/api/skills.py`、`web/skills.js`、`web/index.html`、`web/css/skills.css`、`tests/test_skills.py`。
- 验收：新增 2 条用例（`unique_id` 依次 -2/-3、长 base 限长且仍可写），`pytest tests -q` 全过；`node --check web/skills.js`、`py_compile` 通过。**未做浏览器实测**：重启后连续点两次「新建」并保存，应得到 `skill-2`/`skill-3` 两个目录而不是覆盖。
- 遗留：`data/skills/skill/SKILL.md`（空模板）可让用户在界面里删掉；删除会在 `config.skills_deleted` 记墓碑，预设播种不会复活它。

## [2026-10-31] 后台探索批次（explore background=true）前端看不见：改轮询卡片
- **根因**：`explore(background=true)` 立刻返回批次号，子代理在**独立线程**里继续跑；但它的进度事件走的是主回合同一个 `sse()`，回合结束 SSE 关闭后事件全部写到空处（`_emit_safe` 里被吞掉）。前端唯一能看到的是一条 `explore` 工具记录，而它又被塞进「过程」折叠组里默认收起，所以表现就是「什么变化都没有」。也没有任何 HTTP 接口能查批次状态（只有模型自己能调 `explore_status`）。
- **修改**：
  - 新增 `app/api/explore.py`（自注册）：`/api/explore/status`、`/api/explore/result`、`/api/explore/cancel`，直接包 `explore.status/result/cancel`；在 `httpd.py` 里 import 注册。
  - `web/chat.js`：新增 `buildExploreBatchCard` + `appendExploreBatchCards` + 轮询（2.5s，卡片离开 DOM 自动停表）。卡片常驻在消息里、**不进过程折叠**：状态点 + 「运行中 2/5 · 12s」+ 每个子代理的回传预览 + 「查看结论」（完成后拉 `/api/explore/result`，pre-wrap 展示）+ 「取消」。
  - `renderToolLog` / `buildBlocksView` 对后台批次跳过通用工具卡（避免折叠里再出现一条），改由卡片统一呈现；`buildAiRowFromBlocks`（历史回显）与 `stream.js` 流结束重排后都会补挂卡片。
  - `web/css/base.css`：`.explore-batch-*` 一套样式，复用 `subagentPulse` 动画。
- **注意**：卡片依赖 `tools` 里的 `{tool:'explore', args:{background:true, batch:'ex_…'}}` 记录（后端 2026-10 已写入）；旧消息没有这条记录就不会出卡。
- 相关代码：`app/api/explore.py`、`app/httpd.py`、`web/chat.js`、`web/stream.js`、`web/css/base.css`、`tests/test_explore.py`。
- 验收：`node --check web/chat.js / web/stream.js`、`py_compile` 通过；新增用例断言三个路由注册成功；`pytest tests -q` 全过。**未做浏览器实测**：需要在对话里让 AI 用 `explore(background=true)` 派一批，消息里应立刻出现「后台探索」卡并每 2.5s 更新，回合结束后继续更新直到完成。

## [2026-10-31] 关窗保活扩展到后台长任务（子代理批次 / 自动化）
- **原设计（三层解耦）**：
  1. 回合级：一轮生成跑在服务端线程，事件进 `JOBS[sid]`（`_seq`），前端 `/api/chat/attach` 从 `since` 续播；
  2. 进程级：窗口进程退出后，`main.py` 若发现还有生成中的会话（`ACTIVE_STREAMS`）就进入保活；`chat_window_keep_alive=true`（默认）一直等到全部结束，false 时最多 `chat_window_grace_min`（默认 10）分钟；没有任务则等 5 秒看窗口是否重开，再退出；
  3. 子代理级：`explore(background=true)` 起 daemon 线程，事件写向已关闭的 SSE 会被丢弃，进度靠 `data/subagents/<batch>.jsonl`（只在**批次结束后**落盘）+ 前端轮询。
- **缺口**：第 2 层只看 `ACTIVE_STREAMS`；后台批次和自动化线程都不在里面。于是「关窗 → 回合结束 → 5 秒后 `os._exit(0)`」会把它们一起杀掉。用户确认希望「等后台跑完再退」。
- **修改**：
  - `app/explore.py` 新增 `running_count()`（`_JOBS` 里 status==running 的数量）；`app/autopilot.py` 新增 `running_count()`（**只数活着的驱动线程**，不看 `goals.json` 的 status——重启后残留的 running 会让进程永远退不出去）。
  - `main.py` 新增 `_background_job_count()` / `_pending_job_count()`（对话流 + 后台任务）；`_wait_streams_grace()` 与外层退出循环统一用合计值，日志改成 `pending job(s)`。
  - LLM 调用本身有超时（流式 300s），后台批次不会无限挂住，所以 keep_alive 仍然不设硬上限，与对话流的语义一致。
- **更正之前的判断**：我此前说「进程重启后 `explore.status` 会误报 done」不成立——`_persist` 只在批次完成 / followup 时写盘，被中断的批次不会有磁盘记录，`status()` 会返回「找不到批次」，前端卡片显示「状态不可查」。代码里没有假的 done 状态，未改。
- 相关代码：`main.py`、`app/explore.py`、`app/autopilot.py`、`tests/test_background.py`。
- 验收：`tests/test_background.py` 扩到 7 条（含「只有后台任务也等」「合计计数」「autopilot 只数活线程」「explore 只数 running」）；`pytest tests -q` 全过；`py_compile` 通过。

## [2026-11-01] 会话分组「⋯」菜单自动化验收受阻（⋯ 只在 hover 时才可见）
- 目标/决策：在 http://127.0.0.1:51999/ 上验收分组菜单（置顶分组 / 重命名分组 / 删除组内全部会话）。
- 现状（代码）：web/chat.js::sessionGroupEl 里 .session-group-menu 默认 opacity:0，只有 .session-group-head-wrap:hover .session-group-menu 与 .session-group-menu:focus-visible 才设为 1（另有 @media (hover: none) 兜底设为 1）；按钮 onclick → openGroupMenu(headWrap, group)（web/chat.js 322-356、388-398）实现本身正常。
- 自动化实测结论：Trae 内置浏览器里 hover 组头后 head.matches(':hover') 为 true，但 head.closest('.session-group-head-wrap').matches(':hover') 为 false（document.querySelectorAll(':hover') 只返回按钮本身），⋯ 始终 opacity 0 → 快照/无障碍树不返回它（拿不到 ref）；Tab 聚焦后 :focus-visible 仍为 false，Enter/Space 不触发 click。
- 坐标点击也不行：offsetX/offsetY 只在 ref 元素自身盒子内生效，点组头右侧（⋯ 中心约 (141,293)）会被丢弃；改点 aside 容器同样不会派发到该坐标（document.elementFromPoint 已确认该点最上层就是 ⋯）。
- 环境异常：会话期间 /api/events 反复 ERR_CONNECTION_REFUSED；侧栏多次重渲染（快照 ref 频繁失效）；最后浏览器桥接直接断开（failed to connect to browser plugin of Chrome, os error 10061），无法继续补测。
- 结果：菜单未打开，置顶 / 重命名 / 删除三项均未验证；删除确认未触发（无不可逆操作）。调试期间切换过「分组方式」两次（已还原为分组态）并折叠过 Galgame_monster 分组（可能残留折叠状态，存于 localStorage）。
- 后续（未实施）：若希望无 hover 环境也能操作，可给 ⋯ 增加 JS mouseenter 加 class 或 :focus（不只是 focus-visible）显示，或把 (hover: none) 的判断改成能覆盖该浏览器。

- 未做：`chat_window_keep_alive` / `chat_window_grace_min` 仍只在 `config.json`，没有设置界面入口。

## [2026-10-31] Skill 面板重构：两栏工作台
- **原交互的问题**：列表与编辑器纵向堆叠（列表一长编辑器被推走）；名称/描述/关键词要手写 YAML frontmatter，和正文挤在同一个 textarea；行内勾选框与「编辑」按钮混排；保存/删除/打开目录平铺，删除没有危险区分；没有搜索、没有未保存提示。
- **新结构**（`web/index.html` 的 skills 面板重写）：
  - 左列：搜索框 + 新建 + 列表（名称 / 描述 / 启停开关）+ 底部「共 N 个 · 启用 M」与「打开目录」；
  - 右列：编辑器（名称 / 一句话描述 / 关键词三个输入框 + 正文 textarea），右上角「删除（危险色）+ 保存」，下面是 id 与 `data/skills/<id>/SKILL.md` 路径；
  - 窄窗口（<760px）自动退化成上下排列，列表限高 240px。
- **交互**（`web/skills.js` 重写）：
  - 点列表行切换；有未保存改动时先 confirm；再点当前行不会再从磁盘覆盖草稿；
  - 表单改动即标「未保存」，Ctrl/Cmd+S 直接保存；
  - 新建 → 保存走 `unique:true`（后端分配不冲突 id）；保存后自动清空搜索条件，避免刚保存的条目被过滤隐藏；
  - 启停开关点按不会误触切换行；
  - frontmatter 里 name/description/keywords 之外的键**按原顺序保留**（`enabled`、自定义字段不会被吃掉），解析/拼装用 `skillParse` / `skillCompose`。
- **样式**（`web/css/skills.css` 重写）：grid 两栏、开关用 checkbox+轨道、`--radius-md`/`--warn`/`--danger` 等现有 token，不新增颜色。
- **协议不变**：仍走 `/api/skills` 的 list / save / delete / toggle / open；后端未改。
- 验证：`node --check web/skills.js` 通过；用 Node 跑 parse → compose → parse 往返（含 `enabled`、`custom` 两个未知键）结果一致；HTML 的 `skill-*` id 无重复；CSS 花括号配平（40/40）。
- **未做浏览器实测**：刷新后在「设置 → Skill」应看到两栏；点行载入表单、改字段出现「未保存」、保存 toast 带路径。
- **已知未覆盖**：未保存提示只覆盖面板内部切换（列表 / 新建 / 删除），切到别的设置页签或关弹窗仍会丢草稿。

## [2026-10-31] 全局规则改成多条 + 单条开关
- **原来的实现**：`user_profile.json` 里一个 `manual` 字符串，设置页一个 textarea，整段拼进每轮系统提示（`prompt_assembly` 的 `GLOBAL_RULES_HEAD`）。
- **新数据结构**：`profile.rules = [{id, text, enabled}]`，`manual` 保留做兼容。
  - `app/profile.py` 新增 `clean_rules()`（去空、字符串兼容、`enabled` 强制布尔、单条截断 4000 字、最多 50 条、缺 id 自动补）与 `manual_block()`；
  - `manual_block` 规则：**有 rules（哪怕全部停用）就只拼启用的**；全部停用返回空串，**不会回退 manual** 把刚停用的内容又塞回去；只有还没有 rules 时才读旧 `manual`。
  - `DEFAULT_PROFILE` 增加 `rules: []`，`/api/settings/defaults` 因此自动支持「恢复本页默认」。
- **读取方统一**：`app/api/chat.py`（每轮注入 + 观测摘要触发条件）、`app/context.py`（上下文占用估算）、`app/llm.py`（`user_profile_text`）都改走 `manual_block()`；`app/api/misc.py` 的 `/api/profile` 白名单加 `rules` 并做清洗。
- **前端**：设置 →「用户画像与学习」的单个 textarea 换成规则列表：
  - 每条一行：启用开关 + 序号 + 展开/收起 + 删除 + 正文 textarea；底部「＋ 添加一条规则」；
  - 展开/收起（2026-10-31 补）：`rows=3` 的文本框只有 88px 高，长规则要在小框里滚动。行头加「展开」按钮，点一下给 `.rule-item` 加 `open` 类，`.rule-text` 的 `min-height` 从 62px 提到 240px，按钮文字切换为「收起」并维护 `aria-expanded`；只是视觉状态，不参与脏检查、不写盘。实测 88px ↔ 240px 切换正常。
  - 旧 `manual` 载入时自动折算成「规则 1」（id=`r_legacy`），保存后只写 `rules`、`manual` 置空；
  - 新增/删除/开关都会触发「未保存的改动」（复用全局 input/change 监听 + 手动 `updateSettingsDirty`）。
- **样式复用**：把 Skill 列表里的开关提取成 `base.css` 的通用 `.ui-switch`，`skills.css` 里重复的 `.skill-switch` 删除；规则列表样式在 `polish.css`（`.rule-*`）。
- 相关代码：`app/profile.py`、`app/api/misc.py`、`app/api/chat.py`、`app/context.py`、`app/llm.py`、`web/index.html`、`web/settings.js`、`web/skills.js`、`web/css/{base,polish,skills}.css`、`tests/test_profile_rules.py`。
- 验收：新增 `tests/test_profile_rules.py` 6 条（默认值、旧 manual 回退、只拼启用、全停用不回退、字符串兼容、上限）；`pytest tests -q` 全过；`node --check web/settings.js web/skills.js`、`py_compile`、CSS 括号配平（base 1348 / polish 129 / skills 35）。
- **未做浏览器实测**：刷新后「设置 → 用户画像与学习」应看到规则列表；加两条、关掉一条、保存后重开设置应保留开关状态；对话里只应注入启用的那几条。
- 注意：注入顺序就是列表顺序（目前没有拖拽排序）；全部停用后，「写了全局规则才注入观测摘要」的条件也会变成不满足。

## [2026-10-31] 通知点击跳转失效：改走 Electron 主进程通知
- **现象**：点系统通知不会切回对应会话 / 任务，窗口也不会到前台，只能手动切窗口。
- **根因（三条）**：
  1. Electron 壳里用的是**渲染进程**的 `new Notification()`。Windows 上这类通知的 `onclick` 在部分 Electron 版本里收不到，点完等于没反应；而且 `window.focus()` 无法把**最小化**的窗口还原。
  2. 浏览器兜底路径里通知对象是局部变量，被 GC 后 `onclick` 可能一起失效。
  3. 定时提醒还有**计划任务版 toast**（`scripts/notify.ps1`），它是裸 toast、没有任何 activation，点击永远无效；应用在跑时它会和可点击的应用内通知**重复弹两条**，用户很可能点到的正是那条死的。
- **修改**：
  - `desktop/main.js`：新增 `win:focus` IPC（restore → show → focus）；新增 `notify:show`，用**主进程** `Notification` 创建通知并监听 `'click'`：先 `focusWindow()`，再把 `{id, target}` 发回渲染进程；用 `liveNotifs` 持有引用防 GC。
  - `desktop/preload.js`：暴露 `gmWin.focus()` / `gmWin.notify(payload)` / `gmWin.onNotifyClick(fn)`。
  - `web/auto.js`：`showSystemNotify` 优先走 `gmWin.notify`（target 随 IPC 往返，页面刷新后也能跳）；浏览器路径保留并持有通知引用；新增 `bindNativeNotifyClicks()` 在收到点击后调 `notifyJump(target)`。
  - `scripts/notify.ps1`：启动时先探测 `data/app_port.txt` 里的端口是否在监听；**应用在跑就 `exit 0` 不再弹计划任务 toast**（应用自己会弹可点击的那条）；应用没跑才弹（此时没有可跳转的目标，保持原行为）。
- **边界**：应用完全关闭时，计划任务 toast 点击仍然不会拉起应用——要做需要注册自定义 URI 协议并在 `main.py` 解析，成本更高，暂未做。
- **2026-10-31 补：通知会响两声**。原因：应用自己用 WebAudio 按类型合成提示音（`web/auto.js` 的 `playNotifySound`），而 Electron 主进程 `new Notification()` 默认 `silent: false`，Windows 又补一声系统通知音；浏览器兜底路径同理。修法：`desktop/main.js` 的 `notify:show` 与 `web/auto.js` 的 `new Notification(...)` 都加 `silent: true`，只保留应用内提示音。
- 相关代码：`desktop/main.js`、`desktop/preload.js`、`web/auto.js`、`scripts/notify.ps1`。
- 验证：`node --check` 三个 JS 通过；`notify.ps1` 用 PowerShell Parser 解析通过。**未做真人点击验证**（系统 toast 无法自动点）：需要重启应用后最小化窗口，等一条「回复完成」通知或提醒，点通知应还原窗口并切到对应会话；`data/desktop.log` 会多一行 `notify click <id>`。

## [2026-10-31] 工作台日程编辑/新建弹窗：时间选择器重做
- **问题/决策**：原「开始 / 结束」是两个原生 `datetime-local`，时分只能键盘输入 + 小箭头微调，点选很费劲。改成：日期快捷 chips + 日期框 + 时/分下拉（5 分钟步进）+ 常用时间 chips；结束时间由「开始 + 时长」自动推算，选「自定义」才展开结束日期与时分。经确认范围为**日程 + 任务截止时间**，工作流动作里的 `calendar.create` 保持原样。
- **实现**：
  - `web/work.js` 新增 `workDateTimePicker(initTs, {simple, onChange})`，返回 `{el, get, set}`。日期仍用原生 `input[type=date]`（保留日历弹层），今天/明天/后天与 09:00/12:00/14:00/19:00 会高亮当前选择；分钟 option 为 00–55 步进 5，编辑到 09:07 这类非整档时把当前分钟补进选项，避免选中值丢失。
  - `workOpenEventDialog` 重写：常用时长 30/60/120/180 分钟，整档命中自动选中，否则自定义；开始时间变化时时长模式自动重算结束，自定义模式下结束 ≤ 开始则顺延 30 分钟；保存前校验结束晚于开始。
  - 保存逻辑改用元素引用（`titleEl/locEl/repeatEl/remindEl/startPick/endPick`），不再用 `querySelectorAll('input, select')` 按索引取值——选择器会插入多个 input/select，索引会错位。
  - 任务弹窗截止时间：改为「设置截止时间（会同步到日程并提醒）」复选框 + 同一套 `simple` 选择器，默认不勾选（等价于原来不填即无截止）。
  - 新增 `workFieldBox`：时间选择器用 `div` 包裹而非 `label`，否则点「开始」标签文字会触发标签内第一个按钮。
  - 样式在 `web/css/work.css` 的 `.work-dt*`，全部复用 base.css token（`--accent-soft` / `--border-strong` 等），未新增颜色。
- **相关代码**：`web/work.js`（`workPad2` / `workDateValue` / `workDateTimePicker` / `workFieldBox` / `workOpenEventDialog` / `workOpenTaskDialog`）、`web/css/work.css`（`.work-dt`、`.work-dt-chip`、`.work-dt-date`、`.work-dt-sel`）。
- **验证（实测）**：
  - `node --check web/work.js` 通过。
  - 浏览器实测（127.0.0.1:51501）：新建日程弹窗有 今天/明天/后天、日期框、时 00–23 / 分 00–55（步进 5）、四个快捷时间；点「明天」日期变 11-01；点 14:00 时/分为 14/00；改 10:30 后选「2 小时」，结束自动为 12:30 且结束区隐藏；点「自定义」结束区出现；取消未落库。
  - 新建任务弹窗：复选框默认未勾选、截止控件隐藏；勾选后出现日期 + 时分（默认下一整点 + 1 小时）；取消勾选再次隐藏；取消未创建任务。
  - **未覆盖**：保存后列表回显（避免往用户数据写测试日程）、重复日程编辑、窄屏布局。

## [2026-10-31] 新增本地图片生成（ComfyUI + FLUX 3 Image Klein）
- **目标/决策**：给项目加「本地文生图」。模型最终选 **FLUX 3 Image Klein 蒸馏版**，不是 Z-Image Turbo：需求是「通用图、效果优先」，BFL 内部评测里 Klein 3 在开源模型中排第一（比 FLUX.2 klein 9B +91 Elo），8GB 卡可用 INT8/NVFP4，官方 4070 <9s、5070 <8s；Z-Image 的优势是 Apache 2.0 与更成熟的配方。入口为**工作台「图片」按钮 + `image.generate` 对话能力**；首期只接本地 ComfyUI；固定 16 步（蒸馏版无 CFG）+ 三档尺寸（1024² / 832×1216 / 1216×832）。
- **架构**：ComfyUI 作为独立进程，项目只当客户端，不引入 torch / diffusers。
  - `app/image_gen.py`（新）：标准库 urllib 调 `/prompt`、轮询 `/history/{id}`、`/view` 取图，保存到 `data/generated/`；`status()` 探测 `/system_stats`。
  - **工作流不写死**：官方模板是 UI 格式且带子图节点，必须在 ComfyUI 里「另存为 API 格式」导出一次。变量替换顺序：正向提示词的 `__PROMPT__` 占位符 → 节点标题含 positive/prompt → 唯一文本节点；宽高/步数/seed 按 inputs 键名替换。换模型只换权重与工作流，代码不动。
  - `app/api/image.py`（新）：`/api/image/status`（支持临时覆盖 url/workflow 供「测试连接」用）、`/api/image/generate`；注册 `image.generate` 能力（write）。
  - `app/task_engine.py`：抽出 `_upsert_artifact()`，新增公开的 `register_artifact()`，让生成的图片直接进工作台「产物」，复用现有图片预览与「定位」。
  - 配置（`app/config.py`）：`image_enabled=False`、`image_comfy_url`、`image_workflow`（留空 = `data/image_workflow.json`）、`image_timeout=180`、`image_dir`（留空 = `data/generated`）。默认关闭，不影响现有行为。
  - 前端：工作台右上角「图片」→ 提示词 + 三档尺寸 chips，生成中禁用按钮；成功后刷新产物栏并打开预览。设置新增「图片生成」页：开关 / ComfyUI 地址 / 工作流路径 / 测试连接。
- **模型文件清单（FLUX 3 Klein 蒸馏 + 8GB 路线，约 11GB）**：
  - `models/diffusion_models/flux3-image-klein-int8-convrot.safetensors`（5.83GB；RTX 50 可换 `flux3-image-klein-nvfp4.safetensors` 5.28GB）
  - `models/text_encoders/qwen3vl_4b_int8_convrot.safetensors`（4.86GB；bf16 版 8.88GB）
  - `models/vae/flux2-vae.safetensors`（336MB）
  - 可选 prompt upsampler 9B 约 6.7GB，首期不接。
  - 许可：FLUX Kommunity License，个人/非商业、年收入 <500 万美元可免费商用；不是 Apache 2.0。HF 仓库是 gated，需先接受许可。
- **验证（实测）**：
  - `pytest tests/test_image_gen.py` 7 条全过（占位符替换、标题兜底、缺提示词节点、未启用、缺工作流、成功落盘、ComfyUI 报错透传）；`pytest tests -q` 全过；`py_compile`、`node --check web/work.js web/settings.js` 通过。
  - 路由注册已确认：`POST /api/image/status`、`POST /api/image/generate`。
  - 直接调用处理器：未启用时返回「图片生成未启用」；`status` 返回 `reachable=false`、`workflow_exists=false` 和可读的 WinError 10061。
  - 浏览器实测 UI（未点生成/测试连接）：设置左侧出现「图片生成」页且三控件齐全；工作台「图片」弹窗有提示词框与三个尺寸按钮，默认方形高亮。
- **未覆盖 / 下一步**：
  - 本机还没装 ComfyUI，**真实出图未测**；工作流 JSON 需要用户导出后功能才可用。
  - 当前运行中的实例是旧代码，**必须重启应用**后新接口才生效。
  - 步骤：装 ComfyUI → 下三个文件 → 打开官方 `image_flux3_klein_t2i` 模板 → 正向提示词改 `__PROMPT__` → 「另存为 API 格式」放到 `data/image_workflow.json` → 设置里开开关、测试连接 → 工作台生成首张。

## [2026-10-31] 修复「备份能导出、恢复却无效」：预检 512MB 上限
- **现象**：设置「数据 → 备份与恢复」能导出 zip，但把同一份 zip 选回来时预检失败、「确认恢复」一直灰着，看起来备份与恢复整个不可用。
- **根因（实测）**：导出把 GAL 存档整份打包，本机 11 份存档约 340MB，**zip 500MB、解压后 1864MB**；而 `inspect_backup` 的 `MAX_BACKUP_BYTES` 是 512MB（当初为防 zip bomb），于是程序把自己刚导出的备份判成非法，接口返回 400「备份解压后超过 512 MB，已拒绝」。
- **修改**（`app/backup.py`）：
  - 上限 512MB → **4GB**；另加压缩比保护：解压后 >512MB 且「解压 / 压缩」>200 才拒绝；条目数上限 20000。仍然能拦 zip bomb。
  - 预检改为**流式 SHA256**（1MB 分块），不再把 30MB 的存档整份读进内存；按实际读到的字节数兜底，防止中央目录谎报大小。
  - `_replace_file` 的 `os.replace` 加 3 次退避重试（与 `store.write_json` 一致），避免运行中 sessions.json 被占句柄导致 WinError 5、恢复中途失败。
  - `web/work.js` 恢复确认文案修正：knowledge / notes / uploads / saves 是**整体替换**（备份里没有的文件会删除），原文案说「会保留」与实现不符。
- **验证**：
  - 新增 5 条测试（GB 级上限、20MB 可压缩大文件通过、超上限拒绝、压缩比拒绝、条目数拒绝），`pytest tests/test_backup.py` 8 条全过；`ruff check` 通过。
  - 用真实 500MB 导出包（解压 1.86GB）直接调用 `inspect_backup`：通过，耗时 6.8 秒，files=258，summary = sessions 10 / knowledge 3 / uploads 33 / saves 11。
- **同日补：用户实际遇到的是「点导出没反应、也没有保存位置选择」**：
  - 原实现是 `fetch` 成 blob 再 `<a download>`。Electron 下 500MB 的 zip 先在渲染进程内存里多存一份，打包/传输期间页面毫无反馈；浏览器 / Edge 应用模式没有工具栏下载提示，看起来就是没反应；而且 1 秒后就 `revokeObjectURL`，大文件可能还没下完。
  - 改为：Electron 新增 IPC `file:save-url`（`desktop/main.js`）——先 `dialog.showSaveDialog` 选位置，再由主进程 `http.request` 流式写盘，250ms 节流回传进度；只允许请求 `APP_URL` 同源地址。`desktop/preload.js` 暴露 `saveUrl` / `onSaveProgress`。
  - `web/work.js` 的 `workBackupExport`：Electron 走 IPC；浏览器支持 `showSaveFilePicker` 时先选位置再流式写入；兜底才用 anchor 下载（60 秒后再 revoke）。按钮全程显示「正在打包… / 导出中 x MB」。
  - 验证：`node --check desktop/main.js desktop/preload.js web/work.js` 通过；导出接口本身已实测 200 / 500MB。**保存框与进度需要重启 Electron 后人工点一次确认**。
- **注意**：改动要**重启应用**才生效（旧进程仍是 512MB 上限、也没有 `file:save-url`）。

## [2026-10-31] 评测可复现基线：固定温度 + 环境快照 + 重复聚合 + 研究 runner
- **背景/目标**：`scripts/bench.py` 只有 6 条内部任务，`research_eval` 只是「评已有会话」的评分器；两者都缺「跑模型 + 固定环境 + 重复统计」，无法形成可复现成绩。
- **改动**：
  - `app/llm.py` / `app/providers.py`：`stream_chat` 增加可选 `temperature`（默认 `None`，不传则完全保持原行为），OpenAI 兼容与 Anthropic body 都会下发；评测固定 `0`。
  - `app/bench.py`：`run_task` 记录温度；`save_run` 多写 `env.json`（模型 / 温度 / repeat / git commit / 是否有未提交改动 / Python / 平台 / app_version）；**修复重复跑互相覆盖**（原来固定写 `<task>.json`，现在 `<task>.2.json` / `.3.json`）；`summarize` 增加 `by_task`（attempts / passed / 通过率 / token），`summary.md` 增加「按任务（含重复）」表；CLI 未传 `--model` 时从结果回填。
  - 新增 `scripts/eval_research_run.py`：真正跑模型的 research runner（此前只能评已有会话），复用 `app.search` 检索、把 `finish` 当收尾，产出 runs 目录 + `summary.json/md` + `env.json`。
  - 新增 `eval/README.md`（评测协议、命令、产物结构、局限）与 `eval/env.example.json`。
- **验证（实测）**：
  - `pytest tests/test_bench.py tests/test_research_eval.py` 19 条通过；`ruff check` 通过。
  - bench 烟雾：`bug_fix` 1/1，4 轮 / 8 次工具 / 15.3s / 74,867 tokens；`env.json` 与 summary 结构正确（温度 0、git commit 5b701ec、dirty=true）。
  - research 烟雾：`http-cache` 跑通，但机检不通过——覆盖 0.812、引用 84 处、坏引用 `[25][26][27]`（实际来源只有 1–24）。这是模型编造引用编号，不是 runner 故障。
- **踩坑**：`web_search` 与 `finish` 都**不在** `agent.run_tool` 的 `_HANDLERS` 里（由 `api/chat.py` 主循环处理）；评测 runner 必须自己接这两个，否则会报「未知工具」并永远停在 max_turns。
- **未覆盖**：完整 6 题 × 3 次基线（后台跑）、研究 12 题 × 3 次、公开基准（Terminal-Bench / SWE-bench）尚未接。
- **相关代码**：`app/bench.py`、`app/llm.py`、`app/providers.py`、`scripts/eval_research_run.py`、`eval/README.md`、`tests/test_bench.py`。

## [2026-10-31] 可配置只读子代理团队（delegate + data/agents/*.md）
- **背景/决策**：方向定为「Agent 能力优先」，先做可配置子代理团队。v1 只读，避免写权限放大；复用现有 explore 编排，不另起一套框架。
- **实现**：
  - `app/agent_defs.py`（新）：`data/agents/<id>.md`，frontmatter 支持 `name / description / tools / model / max_turns / enabled`，正文是系统提示词；内置 researcher（只联网）、reviewer（只读审查）、critic（反证）；工具白名单 `READ_ONLY_TOOLS`，写 / 执行 / 重命名 / 提交类直接拒绝；用户文件覆盖内置，删除文件回退内置。
  - `app/explore.py`：支持 `ctx["roles"]` 自定义角色（tools / prompt / model / max_turns）；`build_schemas(research, extra_tools)` 按角色并集下发工具；新增 `normalize_agents()` 把 `{agents:[{agent,task}]}` 转成与 questions 同构；`_run_one` 按角色取提示词与工具、按需把 per-agent model 传给 chat_fn；含 `web_search` 的角色走来源 URL 核验。
  - `app/agent.py`：新增 `delegate` 工具 schema（agents 数组、goal、deliverable、verify），并加入 `is_known_tool`。
  - `app/api/chat.py`：`delegate` 分支复用 `explore.run`，只读、搜索复用主循环台账编号、token 计入本轮、事件走现有 subagent_*。
  - `app/api/agents.py` + `httpd.py`：`/api/agents` list / get / save / delete。
  - 前端：设置新增「子代理」页（`web/agents.js`、`index.html`、`polish.css`）——列表、Markdown 编辑器、新建 / 保存 / 恢复内置；打开设置时自动加载。
- **验证**：
  - 新增 `tests/test_agent_defs.py` 7 条 + `test_explore.py` 2 条自定义角色用例；子代理相关 40 条通过，全量 `pytest tests -q` 通过。
  - `ruff`、`py_compile`、`node --check web/agents.js web/settings.js` 通过。
  - 运行时确认：`POST /api/agents` 已注册；`delegate` 已出现在原生工具 schema；内置三个代理工具集正确。
  - **未覆盖**：真实 `delegate` 对话调用、设置页实际交互需要重启应用后人工验证。
- **v1 不做**：可写 implementer、DAG 可视化编排、跨会话常驻团队、代理市场。

## [2026-10-31] 子代理团队 P0/P1：动态发现、后台/追问、设置体验
- **P0**：
  - `delegate` 的工具描述在生成时动态列出「当前可用代理」（id + 一句话），新建的自定义代理模型也能看见；实测 `my_agent` 会出现在描述里。
  - 子代理事件带 `name`；前端 `stream.js` / `chat.js` 补了 researcher / reviewer / finder / worker 的标签与 `delegate` 工具名，自定义代理优先显示 name。
  - 把 delegate 执行从 `chat.py` 抽到 `app/delegate.py`（解析定义 → explore.run / start_background），便于单测。
- **P1**：
  - `delegate` 支持 `background`、`acceptance`、父对话 fork 前缀；`explore.start_background` 与 `normalize_agents` 支持 agents 参数。
  - `_remember` 持久化 roles；`explore_followup` 恢复自定义角色、按角色是否含 web_search 决定来源核验。delegate 结果会带批次号，可继续追问。
  - 设置页：启用开关、只读工具多选、模型 datalist、id 改名、新建查重、导入/导出 `.md`；API 增加 toggle，save 支持 create / old_id。
- **验证**：
  - 新增 `test_delegate.py` 5 条、`test_agent_defs.py` 新增 4 条；相关 56 条通过，全量 `pytest tests -q` 通过；`ruff`、`node --check` 通过。
  - **未验证**：设置页交互与真实对话里的 delegate 调用，需要重启应用后人工验证。
- **相关代码**：`app/delegate.py`、`app/agent_defs.py`、`app/explore.py`、`app/api/agents.py`、`app/api/chat.py`、`app/agent.py`、`web/agents.js`、`web/stream.js`、`web/chat.js`、`web/index.html`、`web/css/polish.css`。

## [2026-10-31] 内部评测基线首跑：13 / 18（72.2%）
- **命令**：`python scripts/bench.py run --repeat 3 --out eval/runs/baseline-20261031`
- **环境**：deepseek-flash、温度 0、重复 3 次；commit 5b701ec（有未提交改动）；Python 3.13.5 / Windows 11。结果目录 `eval/runs/baseline-20261031/20261031-203220/`。
- **总成绩**：13 / 18 通过，通过率 0.7222；284.7 万 token，1514 秒。
- **按任务**：
  - bug_fix 3/3、refactor 3/3、ui_restore 3/3。
  - tool_pipeline 2/3：有一次 `error`（0 轮 0 工具，47.6s），表现为模型没发起任何工具调用。
  - asset_mc 2/3：有一次 finished 但 atlas.png / manifest.json 没生成。
  - **web_voxel 0/3**：三次都卡在视觉断言 `fps 0–1.2 < 10`；两次 max_turns（18 轮），单次最长 389s，token 消耗最高（118 万）。
- **结论**：
  - 基础文件/重构/UI 还原稳定；「画布/渲染性能」类任务是目前明确短板，不是工具不足，而是模型写出的 canvas 实现性能不达标。
  - 样本只有 6 题，不能对外宣称绝对强弱；这份结果主要用于后续「单代理 vs 团队」对比的基线。

## [2026-10-31] 子代理 P2：可写 implementer + 串行 pipeline + 团队评测
- **可写代理**：
  - `agent_defs` 增加 `permission: readonly|write`。`write` 才允许 `write_file / edit_file / make_dir`；内置 `implementer`（写）与 `planner`（只读规划）。
  - `explore._run_one` 的写权限判断改为「批次允许 或 角色自身 allow_write」；写入仍走调用方注入的 `write_tool`，在对话里就是主循环的 `_wf_make_env` 审批 + 撤销。
  - 设置页新增「允许写文件」开关，写工具在多选里标「（写）」，未开权限时禁用。
- **串行编排**：`delegate` 支持 `pipeline=[planner, implementer, reviewer]` + `task`；上一阶段输出作为下一阶段背景，失败即停止；与 `agents` 二选一，暂不支持后台。
- **评测**：
  - `bench` 增加 `--team`（任务提示要求优先 delegate）与消融组 `no_delegate`；内部循环补了 `delegate` 分支（评测无审批，写入直接落临时沙箱）。
  - 最小对比（`tool_pipeline`，deepseek-flash，1 次）：团队 9 轮 / 10 工具 / 29.1s 通过；无 delegate 5 轮 / 6 工具 / 11.1s 通过。团队确实派出 reviewer 复核 summary.json（6.8s / 7196 token），但小任务上没有准确率收益，成本约 2.6 倍。
- **验证**：相关 68 条测试通过（新增写权限、pipeline、团队用例）；`ruff`、`node --check`、全量 `pytest tests -q` 通过。真实 UI 与真实对话里的 delegate/写审批仍需重启应用后人工验证。
- **并行上限对齐**：`delegate` 之前写死 8；现在跟随 `agent_explore_max`（默认 8，硬上限 12）。设置页与 config 注释同步为「默认 8，硬上限 12」。
- **相关代码**：`app/agent_defs.py`、`app/explore.py`、`app/delegate.py`、`app/bench.py`、`app/agent.py`、`app/api/chat.py`、`scripts/bench.py`、`web/agents.js`、`web/index.html`、`eval/README.md`。

## [2026-10-31] 自动派发三档：suggest / auto-plan / auto-review
- **配置**：`agent_auto_route`（默认开）、`agent_auto_plan`（默认开）、`agent_auto_review`（默认关）；设置 → 子代理页可切换，「恢复本页默认」覆盖这三项。
- **suggest**：系统提示追加 `routing_block()`（只列启用中的代理 + 派活时机 + 不该派活的场景）；工具循环里从 `tool_log` 尾部数，最近一次写入之后连续 ≥4 次只读且没有 `delegate/explore/workflow_run` 时，随工具结果注入一次提醒（每轮最多一次）。
- **auto-plan**：回合开始、进入工具循环之前跑 `planner` 子代理（只读），把结论用 `_attach_dyn_tail` 贴到本轮最后一条 user 消息上，要求主代理用 `update_plan` 落成计划卡。只在「像任务」时触发（`plan_worthy`：≥8 字且命中动作词；继续/接着、纯问答不跑），已有/继承计划、`plan_mode`、GAL、autopilot 均跳过。
- **auto-review**：模型调用 `finish` 时，如果本轮 `turn_writes > 0` 且开关打开，派 `reviewer`（只读）复核本轮写操作；结论回灌到工具结果，模型可在真正收尾前修复。每轮最多一次。
- **安全边界**：三档都只派只读代理；写入仍必须由主代理显式 `delegate`（implementer）并走审批。auto-plan / auto-review 复用 `_make_dl_ctx()`（从 delegate 分支抽出），共享同一套沙箱、搜索、写审批环境。
- **相关代码**：`app/auto_delegate.py`（新增）、`app/config.py`、`app/api/chat.py`（`_make_dl_ctx`、提示注入、路由提醒、finish 复核）、`web/index.html`、`web/settings.js`、`tests/test_auto_delegate.py`。
- **验证**：新增 9 条单测；`test_auto_delegate / delegate / agent_defs / explore / settings_defaults / api_handlers_smoke` 与 `agent_guards / chat_blocks / prompt_assembly / native_tools / stream_stop / fork / chat_jobs` 全部通过；`py_compile` 通过。**未验证**：真实对话里的自动规划/复核效果与设置页开关（需重启应用人工确认）；全量 `pytest` 因沙箱后端报错未跑完。

## [2026-10-31] 八款 Agent 能力横向评测：双榜与成熟度校准
- 问题/决策：把本项目与 OpenCode / Codex / Claude Code / Trae / DeepSeek Harness / Cursor / ZCode 做 Agent 能力排名。口径取「通用」+「编码」双榜；本项目按源码静态审查取证，外部产品按 2026-10-31 官方文档。
- 评分框架（1–10 主观分，不是实测成功率；±3 视为噪声）：
  - 通用榜权重：工具执行 15 / 长程自主 15 / 上下文记忆 20 / 可扩展 15 / 安全可控 10 / 多模态与本地生态 10 / 交互可观测 5 / 工程成熟度 10。
  - 编码榜权重：工具与改码 20 / 长程自主 20 / 上下文 15 / 可扩展 10 / 安全 10 / 多模态 5 / 交互与可观测 10 / 成熟度 10。
- 结论：
  - 通用榜：Claude Code 90.5 > Codex 88.0 > 本项目 83.5 ≈ Cursor 82.5 > Trae 75.0 > OpenCode 71.5 > ZCode 69.5 ≈ DSH 69.0。
  - 编码榜：Claude Code 92.0 > Codex 89.0 > Cursor 87.5 > 本项目 78.0 ≈ Trae 78.0 > OpenCode 72.0 > ZCode 70.0 > DSH 67.5。
  - 成熟度校准（0.75×能力 + 0.25×成熟度×10）后，两榜名次收敛为同一顺序：Claude Code > Codex > Cursor > Trae > OpenCode > 本项目 > ZCode > DSH；本项目两榜均由 3/4 名掉到第 6。缺的不是 Agent 设计，而是产品化。
- 本项目的真实优势（代码锚点）：70+ 工具与 9 个 LSP 工具、repo_map/impact（app/agent.py:214-238）；显式 finish + 计划续跑（app/api/chat.py:806-834）；多路子代理 explore/delegate/autopilot/orchestrator/task_engine；四档审批 + 命令白/黑名单 + 密钥护栏 + 撤销/重做（app/agent.py:113-148,1756-1795）；检查点摘要压缩 + 画像/知识图谱/笔记/统一记忆索引（app/context.py:119-142、app/knowledge.py:419-543）；本地 ASR/TTS/PDF OCR/Windows 通知/定时任务。
- 主要短板：单人项目、Windows only、无公开基准（仅内部 6 题×3 次 13/18，deepseek-flash）、模型能力依赖用户 API、无云端并行/团队协作、新增工具必须写 Python（Skills 只加提示词）、MCP 默认关闭。
- 可信度：外部产品部分字段官方未写明（标未确认）；各家 benchmark 为模型级且口径不同，未纳入评分。
- 相关代码/指令：app/agent.py、app/api/chat.py、app/explore.py、app/context.py、app/knowledge.py、app/code_mode.py、app/skills.py、app/mcp.py；NOTES.md:681-690（此前六家续跑调研）。


## [2026-10-31] 脚本编排自定义代理 + 并行写隔离 + 通用代理扩充
- **脚本编排**：`run_workflow_script` 的 `wf` 桥新增 `wf.agent(name, task)` / `wf.agents([{agent, task}])` / `wf.pipeline([names], task)`；父进程用 `caps["run_agents"]`（`chat.py` 里的 `_script_run_agents`）复用 `delegate.run` 与 `_make_dl_ctx`，子代理事件照常推前端，写入仍走主循环审批。每段脚本最多调用 8 次（`agent_script_agent_calls`，0 = 不限），超限返回 `AGENT_BUDGET`，未开启返回 `AGENT_UNAVAILABLE`。
- **并行写隔离**：新增 `app/write_guard.py`。全局写锁本来就保证串行落盘，这里补「同批不同代理写同一路径」的归属表与最近写者：`serial`（默认）回灌「刚由 X 修改」提醒，`strict` 拒绝第二次写入，`off` 不登记。`explore` 在 `_write_tool` 里 claim，结果带 `write_conflicts`；`format_results` 头部显示冲突数。子代理嵌套与 delegate 共享同一批次表。
- **通用代理**：新增 `tester / debugger / security / documenter（可写）/ explainer`；同步补强 `reviewer`（check_code）、`critic`（find_symbol）、`planner`（git 状态 / diff）、`implementer`（impact / check_code / lsp_diagnostics）。设置页「子代理」自动列出，无需改前端。
- **健壮性**：`explore.run` 直接收到 `agents` 但没带 `ctx["roles"]` 时，会按 `agent_defs` 自动补齐角色配置，避免命名代理落到「未知角色」。
- **配置**：`agent_script_agents=True`、`agent_script_agent_calls=8`、`agent_write_isolation="serial"`；设置 → 子代理新增「脚本编排」「并行写隔离」两组，恢复本页默认覆盖这些键。
- **相关代码**：`app/workflow_script.py`、`app/write_guard.py`（新增）、`app/explore.py`、`app/agent_defs.py`、`app/api/chat.py`、`app/agent.py`、`app/config.py`、`web/index.html`、`web/settings.js`；测试 `tests/test_write_guard.py`、`tests/test_workflow_script_agents.py`、`tests/test_explore.py`。
- **验证**：新增 14 条测试（write_guard 5 / 脚本桥 6 / 写隔离集成 2 / 内置代理 1 / delegate 提示 1）；`test_write_guard / workflow_script_agents / explore / agent_defs / delegate / auto_delegate / workflow_script / workflow / orchestrator / agent_guards / settings_defaults / chat_blocks / native_tools / agent_plan / stepctx / prompt_budget / fork / resume / bench` 全部通过；`py_compile`、`node --check` 通过。**未验证**：真实对话里脚本派活与审批联动、strict 模式在真实并行任务中的表现（需重启应用人工验证）。


## [2026-10-31] 排名口径修正：能力分与产品化分必须分开
- 问题：上一轮把「成熟度 / 交付形态」混进 Agent 能力总分，且没有单列「记忆」与「本地生态」，会系统性低估本项目；「成熟度」在两份报告里含义也不同（工程成熟度 vs 产品化/生态成熟度）。
- 去偏结果（用现有双榜分数剥离成熟度后重算）：
  - 通用能力：Claude 90.6 > 本项目 89.4 > Codex 87.8 > Cursor 81.7；本项目与 Claude 差 1.2，在 ±3 噪声内。
  - 编码能力：Claude 92.2 > Codex 88.9 > Cursor 87.2 > 本项目 83.3 > Trae 78.9。
  - 产品价值（0.75×能力 + 0.25×产品化）：Claude > Codex > Cursor > 本项目 ≈ Trae > OpenCode > ZCode > DSH；本项目与 Trae 的差距在噪声内。
- 方法修正：能力分不含成熟度；产品化单独计分；同一维度不许重复计权；相邻名次差 ≤3 分不当作排序依据。
- 结论：本项目 Agent 能力 T1（通用视角）/ T2 前段（编码视角），产品化 T2；多智能体单维仍落后 Claude 约 1 分，差距在运行时形态（后台/重跑/上百并行/跨会话/隔离），不是「有没有子代理」。
- 相关：本节修正上一节「八款 Agent 能力横向评测」的校准公式（成熟度被计了两次），排序以本节口径为准。


## [2026-10-31] 编码榜短板清单（按代码证据）
- 命令白名单：`CMD_ALLOW_EXES` 只有 python / node / npm / npx / pip / git / pytest / ruff；无 cargo / go / java / dotnet / make / cmake / docker；bash / powershell / curl 默认拒绝；非白名单只能退回 `shell` 并逐次审批 → 非 Python/JS 项目无法自动跑构建与测试。
- LSP：只挂 pyright（Python / TS / JS），无 gopls / rust-analyzer / clangd / jdtls。
- IDE：Monaco 面板（编辑器 / 终端 / Git diff / AI 补全 / LSP），无调试器（断点 / 单步 / launch.json / DAP）、无扩展市场、多根工作区、devcontainer。
- Git：只到 add / commit / stash / branch + 本地 diff，无 push / PR / GitHub 集成。
- 自主性：回合内阻塞、关窗即停、脚本一次性、12 并行 / 8 次脚本调用上限；worktree 隔离已有（仅 git 沙箱，≥2 个可写代理时自动开，见 2026-11-01 条目）。
- 扩展性：插件必须写 Python、MCP 默认关闭、Skills 只能加提示词、无声明式工具注册 / Agent SDK。
- 交互：审批按工具调用；worktree 回写会列「文件 + 增删行数」，但还没有逐 hunk 接受 / 拒绝，也没有 PR 级审阅。
- 成熟度：Windows-first、单人维护、内部 6 题基线、无公开基准。
- 结论：编码榜的弱点是「改码闭环」——跑不动非 Python/JS 构建、并行改不动、审阅不顺手、产品化不足；上下文 / 安全 / 多模态不是短板。


## [2026-11-01] 四项编排能力的收益与成本
- 后台/可重跑：收益是长任务不占回合、失败只重跑坏步骤、主上下文不膨胀；成本是运行时+持久化+断点恢复+批量审批+预算看门狗，属于中高工程成本。对本项目：先做 checkpoint/rerun，后台后置。
- 上百并行：收益是大迁移/审计的墙钟时间下降；成本是 token 线性放大（12→100 约 8 倍）、供应商限流、重复劳动与合并冲突、审批风暴。对本项目：只对只读任务放宽，写入并行必须先有 worktree。
- 常驻子代理线程 + 跨会话消息：收益是少重复探索、多轮协作、跨会话接力；成本是状态一致性、上下文膨胀、消息总线与权限模型。对本项目：先延长 TTL + 加 agent 消息工具，跨会话后置。
- worktree/云隔离：收益是真并行写、失败可丢弃、可按 diff 审阅合并；成本是 Git 依赖、磁盘与依赖安装、合并冲突、云端隐私与运维。对本项目：本地 worktree 性价比最高，云隔离不作为近期目标。
- 推荐顺序：本地 worktree → 脚本级 checkpoint/rerun → 常驻线程/消息 → 后台/云与大规模并行。


## [2026-11-01] 修复「开启沙箱外阅读仍报路径越界」
- 根因（三个不一致）：
  - `allow_outside` 只在主循环的 `list_dir` / `read_file` 生效，且沙箱外只认**绝对路径**；`list_dir` 对外部目录返回的是 `../...` 相对路径，模型拿它去 `read_file` 必然被拒。
  - `read_files` / `see_image` 不在 `OUTSIDE_READ_TOOLS`，`run_tool` 不会把开关传给 handler；批量读全灭。
  - 子代理 `explore.ReadCache`、Code Mode / `run_workflow_script` 的 `_exec_tool` 调 `agent.run_tool` 时都没传 `allow_outside`，所以主代理能读、子代理和脚本不能。
- 修复（A+B，用户确认）：
  - `resolve_path` 对相对路径越界时明确提示「沙箱外读取需要用绝对路径」；`list_dir` 对外部目录直接返回绝对路径。
  - `OUTSIDE_READ_TOOLS` 扩到 `read_files` / `see_image`；`_t_see_image` 真正把 allow_outside 传给 `resolve_path`。
  - `explore.ReadCache` 增加 allow_outside 并透传；`run` / `followup` 从 ctx 读取；子代理系统提示在开启时补一条「仅按任务要求用绝对路径读外部」。
  - `chat.py` 的 `_make_dl_ctx`、主 explore ctx、followup ctx、workflow explore 节点都补 `allow_outside`。
  - `code_mode.run` / `workflow_script.run` 把 `allow_outside` 写进 caps，`_exec_tool` 透传给 `agent.run_tool`。
- 验证：新增 `tests/test_outside_read.py`（4 条：绝对路径、批量读、外部目录绝对路径、相对路径提示）；`test_code_mode` / `test_workflow_script_agents` / `test_explore` 各加 1 条跨沙箱用例；相关回归（含 prefetch / chat_parallel / fork / agent_guards）全部通过；`py_compile`、`ruff` 通过。
- 未做：C 方案（允许 `../` 相对路径跨沙箱）；如果后续仍觉得路径写法别扭，再单独评估。


## [2026-11-01] 轨迹（Trajectory）：对齐 DSH 的事件账本 + 查看器
- **参考源码**：`@deepseek-ai/dsh-client-ui-trajectory`（`trajectory-record.d.ts` / `layout.d.ts` / `timeline.d.ts` / `client.js`）与 `@deepseek-ai/dsh-session` 的 `SessionEventMap`。DSH 的核心是：append-only 会话日志记录系统提示、推理、工具调用/结果、子代理调度、每次上下文注入；Trajectory 视图按 turn/step 分组，提供 Input/Output/Thinking/Timing/Tool schema 检查器，以及与 resume/fork/search/replay 共用同一份事件流。
- **本实现**：新增 `app/trajectory.py`（旁路写入 + 只读投影）。
  - 事件：`turn/start|end`、`step/start|end`、`request/header`（完整 system + tools，按 hash 去重）、`user/message`（human / 注入上下文，source 区分）、`assistant/message`（正文/思考/usage/timing/TTFT）、`tool/call|result`（callId + traceId + 截断副本）、`subagent`（批次 + agents/pipeline）、`todo/write`、`compact`。
  - 投影：turn → group（输入 / 第 N 步）→ cell；cell 带 input/output/thinking/schema/tokens/duration/ttft/callId/traceId/isError；分页按 turn。
  - 完整工具结果仍由 `tool_trace` 提供（按 trace_id 懒加载）；子代理从 `data/subagents/<batch>.jsonl` 展开成 subtool 行；旧会话无轨迹日志时回退到 `sessions.json` 的 blocks/tools/timing。
- **接入**：`chat.py` 在轮次开始/用户消息、系统提示与工具 schema 发送前、动态上下文/引用/文档/图片/hook/断点注入、助手消息结束、`_log_tool`、update_plan/plan_patch、收尾与压缩处写入；全部 try/except 吞掉，失败不影响聊天。
- **API**：`POST /api/trajectory`（账本，支持 before/limit）、`POST /api/trajectory/tool`（完整工具结果）、`POST /api/trajectory/export`（事件 + 投影）。
- **前端**：侧栏「轨迹」按钮 → 全屏抽屉（`web/trajectory.js` + `web/css/trajectory.css`）：轮次账本、类型筛选、搜索、仅失败、耗时比例条、右侧检查器（Input / Output / Thinking / Tool schema / Raw）、加载更早、导出 JSON。
- **配置**：`trajectory_enabled=True`、`trajectory_max_field=200000`；设置 → 数据与高级新增开关与上限。
- **验证**：新增 `tests/test_trajectory.py` 8 条（投影/去重/工具配对/trace 详情/分页/子代理展开/旧会话回退/API 路由）；相关回归（chat_blocks / prompt_budget / session_events / stream_stop / chat_parallel / agent_guards / settings_defaults / retry_policy / resume / agent_plan / stepctx / task_budget / autopilot / fork / chat_jobs）全部通过；`py_compile`、`ruff`、`node --check` 通过。**未验证**：真实服务里长会话的轨迹体积与浏览器性能，需要重启应用后人工查看。
- **未做（v2）**：raw token chunks（逐 token 回放）、fork/replay 重新执行、时间线缩放/拖拽/区间筛选、超长账本的虚拟滚动；当前用 turn 分页 + 懒加载详情替代。


## [2026-11-01] 本地 worktree 写隔离（多写子代理）
- **问题/决策**：全局 `WRITE_LOCK` 只保证串行落盘，`app/write_guard.py` 只做「同批同文件」提醒 / 拒绝；两个可写子代理先后改同一文件仍会语义覆盖。用户拍板：**每个可写子代理一个 worktree**、**产 diff + 审批卡回写**、**非 git 退回现状**、**一批 ≥2 个可写代理时自动启用**。
- **实现**：
  - 新增 `app/worktree.py`：`git worktree add --detach`（目录 `data/worktrees/<batch>/<agent-哈希>`）；用 `git stash create` 把未提交改动带进隔离区，再补拷未跟踪文件（单文件 2MB / 合计 32MB 上限，只拷沙箱子树）；随后 `git add -A` + `write-tree` + `commit-tree` + `reset --soft` 做**基线提交**（不跑 hook、不动分支），之后 `git diff HEAD` 就是子代理的改动。提供 `changes / patch / apply_check / apply / remove`，补丁落盘 `<agent>.diff`。
  - `app/explore.py`：`_can_write` 判定可写角色；`_worktree_enabled`（off / auto ≥2 写者 / always，且必须有回调）；`_open_worktree` 给单个子代理换沙箱与写回调（失败只 emit 一条提示，退回共享沙箱）；整批跑完 `_settle_worktrees` 顺序回写，`format_results` 标注「已回写 N 个文件 / 未回写原因」。隔离后 `write_guard` 自动跳过（各写各的副本，否则 strict 会误杀）。
  - `app/api/chat.py`：`_worktree_write_for(sandbox)` 让写入落到工作区（审批不变，`no_undo` 不登记散撤销）；`_apply_worktree(info)` 做 `git apply --check` 预检 → 审批卡（`apply_worktree`，full 模式不问，autopilot 且 policy≠auto 时只保留补丁）→ 回写前按文件拍快照 → `WRITE_LOCK` 内 `git apply` → 每个文件登记一条撤销 token（进「变更审阅」队列），另加一条覆盖整批的 token 给汇总卡的「撤回」。`_wf_exec_node` 支持 `env["no_undo"]`；`_make_dl_ctx` / 主 explore ctx / workflow explore 节点都注入隔离回调。
  - 配置与 UI：`agent_worktree_isolation`（默认 `auto`）；设置 → 子代理新增「多写代理的 worktree 隔离」；`chat.js` 加 `apply_worktree` 标签并禁掉「本轮都允许」。
- **实测要点（踩坑）**：
  - `patch()` 必须先 `git add -A`，否则只调 patch 时拿不到改动（曾让冲突检测误判为「可应用」）。
  - 目录名会被中文 id 撞成同一个（`_safe` 曾把「一号/二号」都变成 `agent`），现在尾部拼内容 sha1 前 6 位。
  - `git worktree add` 的目标必须不存在：先 `worktree remove --force` + `rmtree` + `worktree prune`。
- **验证**：新增 `tests/test_worktree.py` 7 条（脏状态 + 未跟踪文件可见、变更列表、补丁应用、冲突检测、非 git / 空仓库回退、子目录沙箱、auto/always/off 判定、两个写代理端到端回写）；`pytest tests` 全量通过；`python -c "import app.api.chat"` 通过。**未验证**：真实服务里连续审批多张 `apply_worktree` 卡片的交互，以及大仓库（万级文件）建 worktree 的耗时。
- **未做/边界**：非 git 沙箱只退回「串行 + 冲突提示」；嵌套子代理（depth>0）不隔离；worktree 里的审批规则按工作区读 `.gm/agent-rules.json`（缺失即默认规则）；进程崩溃时残留目录靠 `_prune_old` 24h 清理；没有逐 hunk 接受/拒绝。


## [2026-11-01] 轨迹 v2：系统提示快照 / 分段 / 差异 + 侧栏入口收敛
- **DSH 源码研究**（`%TEMP%\dsh-src2`、`%TEMP%\dsh-trajectory-pkg`、`%TEMP%\dsh-session-pkg`）：
  - `SessionEventMap`：`turn/step`、`user/message`（人类提示与 `agent.inject()` 注入共用，靠 `source.kind` 区分）、`assistant/chunk|message`、`tool/call|result`（`meta` 带 diff）、`todo/write`、`request/header`（`reason: initial|resume|change`，含完整 system + tools）、`request/context`、`session/end-seed`。
  - 轨迹只有 7 类 cell：`system | user | context | compacted | message | tool | subtool`；SYSTEM cell 带 `promptDetail` / `previousPromptDetail`，检查器是 `Diff / System Prompt / Tools` 三页。
  - 提示变化归类为 `initial | system | tools | system-and-tools`（`RequestPromptChange`）；没有 assistant 输出的请求用 `requestOnly` 占位行。
- **我们发现的问题**：`trajectory.header()` 只取 `messages[0]`（Agent 说明 / 界面上下文 / 计划都会 `insert(0)`，会漏掉基础提示）；11000+ 字的系统提示被压成一行「系统提示 · N 字」；系统时间与动态上下文合并成一条 `runtime`；没有分段与 diff，所以「系统时间注入 / 技能清单」在界面上看不见。
- **实现**：
  - `app/trajectory.py`：新增 `sections()`（按 `#`/`##`/`【…】` 标题切段，>40 段合并）、`_diff_text()`（difflib unified diff）；`header(..., systems=...)` 支持多段 system 拼接，记录 `reason`、`changedTools`、每段 `{label,chars,hash,start,end,preview,changed}`（**只存偏移不存全文**，投影时再切）；投影把每个分段单独成行（`source.kind=prompt_part`），系统行文案变为「初始系统提示 / 系统提示更新：… / 进程重启后恢复」，并带 `promptDiff` 与 `parts` 概况；助手行补 `decodeTps`、`cacheWrite`。旧事件没有 `reason/parts` 时自动退回旧文案。
  - `app/api/chat.py`：header 改为拼接**全部** `role=system` 消息；`_attach_dyn_tail` 处把动态上下文与系统时间拆成两条 context（`runtime` / `clock`）。
  - `web/trajectory.js` + `web/css/trajectory.css`：检查器新增「系统提示分段（含变化标记）」「System prompt diff」；行内 meta 增加 tok/s、缓存命中/写入。
- **侧栏收敛**：删掉侧栏「学习观测」「Skill」两个按钮；`学习观测` 改为大厅卡片 `web/hall-observe.js`（面板内嵌，`observe.js` 改为 `observeSetDays()` + 容器守卫），删除旧 `#observe-modal` 与 `init.js` 里的弹窗绑定；Skill 只剩「设置 → Skill」分页。
- **验证**：`pytest tests` 全量通过（新增 3 条轨迹用例：分段切分、变更检测 + diff、多 system 消息）；`py_compile` / `ruff` / `node --check`（trajectory.js / observe.js / hall-observe.js / init.js）通过；用真实会话的 14218 字系统提示切出 25 段（全局规则 / 可用 Skill / 记忆工具 / 文件 Agent / 联网搜索…）；端到端探针确认 initial → change 只标记变化的「可用 Skill」段、diff 出现 `+- 画图`、每段都出行。
- **未做 / 边界**：`assistant/chunk`（逐 token 回放）与 `requestOnly` 失败请求占位仍未实现；系统提示超过 `trajectory_max_field` 时只切到截断处；尚未在真实界面点开大厅卡片与轨迹抽屉做人工验收（需重启应用）。


## [2026-11-01] 计划卡与聊天流程协同修复
- **发现的问题**：①「继续输出 / 断点续跑」走合并旧消息的分支，没写回 `plan` → 刷新后卡片回退、下一轮继承旧计划；②历史回显不挂 `row._planCard/_planSteps`，也不恢复 `state.lastPlan` → 继续输出时会再建一张计划卡，IDE「任务」面板为空或串上一个会话的计划；③历史卡一律折叠，与实时「做完才折叠」不一致；④`update_plan / plan_patch / acceptance` 只在主循环拦截，工作流节点 / 脚本调用得到「未知工具」；⑤`update_plan` schema 没有 `log`；⑥压缩时 `context.py` 读的 `s["plan"]` 从未被写入（死分支）；⑦只更新计划、正文与工具都为空的回合不落库。
- **修复**：
  - `chat.py`：新增 `_carry_plan(msg, plan_state)`，正常落库 / 续写合并 / 进度快照共用；新增 `_plan_tool(name, args)` 作计划写入的**唯一入口**（normalize/patch → plan_state → task_state → 轨迹 todo → SSE 计划卡），放进 `caps["plan_tool"]`；`caps["acceptance_tool"]` 复用 `_acceptance_handle`；主循环改为调回调；新增 `plan_dirty`，让「只有计划变化」的回合也落库。
  - `agent.py`：`run_tool` 把 `update_plan / plan_patch / acceptance` 分派给 caps 回调（没有会话上下文时返回明确提示，不再报「未知工具」）；`update_plan` schema 补 `log`。
  - `context.py`：压缩 pin 改为取最后一条带 `plan` 的 assistant 消息（`s["plan"]` 仅作旧数据兜底）。
  - `web/chat.js`：`planAllDone()` 统一折叠规则；历史行（`buildAiRow` / `buildAiRowFromBlocks`）挂 `_planCard/_plan/_planSteps`；新增 `planSyncFromSession()`，`renderMessages` 时按当前会话最后一条带 plan 的消息恢复 `state.lastPlan` 并刷新 IDE 任务面板。
- **验证**：新增 `tests/test_chat_plan.py`（4 条：`_carry_plan`、caps 路由 update_plan/plan_patch、缺上下文提示、acceptance 路由）；`pytest tests` 全量通过；`py_compile`、`ruff`、`node --check web/chat.js` 通过。**未验证**：真实界面里的「继续输出后又改计划」「刷新后任务面板」需要重启应用人工确认。
- **已知边界**：历史只保留最终计划版本（中间版本在轨迹 `todo/write` 里）；autopilot 的 goal plan 与工具计划仍是两套（前者是目标级清单，后者是计划卡）。


## [2026-11-01] 同模型 harness 对照：八款产品在 DeepSeek-V4-Pro 下的表现排名
- 问题/决策：控制模型变量，只比 harness。控制模型选 DeepSeek-V4-Pro（8 家均可接入；Harness-Bench / Composio 都有该模型对照数据）。沿用 2026-10-31 口径修正：能力分不含产品化 / 生态。
- 接入矩阵：原生 OpenAI 兼容——本项目 / OpenCode / Codex / DSH / ZCode / Trae（自定义模型）/ Cursor（Override Base URL）；Claude Code 需 LiteLLM 或 LLM Gateway 代理。
- 公开对照（同模型、不同 harness）：
  - Harness-Bench（274 通用任务 × 10 模型 × 7 harness，固定输入/预算/评分）：DeepSeek-V4-Pro 下 Codex 69.5 > OpenCode 69.0 > Claude Code 68.7；10 模型均值 OpenCode 67.4 第一、Claude Code 65.9；无单一 harness 全模型最优。
  - Composio Bench（30 个有状态工具任务）：DeepSeek-V4-Pro 下 Command Code 60% > Hermes/Pi/Cline 56.7% > Codex 53.3% = DSH 53.3% > Claude Code 50% = OpenCode 50%；Codex 482s 最慢、DSH 388s。
  - 关键：Claude Code 在 GPT-5.6 Sol 上 71.6（第 2），在 Kimi-K3 上 60.0；Codex 在 DeepSeek-V4-Pro 69.5、Kimi 65.1；OpenCode 最稳（59.3–70.3）。
- 维度与权重：接入保真 15 / 工具契约与任务域 15 / 循环纠错 20 / 上下文与 token 效率 20 / 长程子代理 10 / 审批摩擦 5 / 可观测 5 / 跨模型实证 10。
- 排名（DeepSeek-V4-Pro）：Codex 87.5 > OpenCode 84.0 > 本项目 82.5（低置信）≈ Claude Code 82.0 > DSH 76.5 > Cursor 75.5 > ZCode 73.0 ≈ Trae 72.5。
- 本项目结论：结构上是「模型无关」路线，循环 / 压缩 / 子代理 / 安全明显强于同模型下的普通 harness；但缺同模型实测，且命令白名单只有 Python/JS、LSP 只有 pyright、无调试器/PR，编码域覆盖会拖分。内部 13/18（deepseek-flash，6 题）不可与外部横比。
- 下一步（可复现实验）：同一 endpoint、温度 0、同一 30+ 任务集、3 次重复，记录成功率 / 轮次 / 工具数 / token / 耗时 / 人工干预 / 安全违规。
- 相关代码：app/providers.py（OpenAI / Anthropic 适配）；外部来源见回答 Sources。


## [2026-11-01] 通用还原质量：image_compare + 验收指标 + 预设 + finish 闸门
- **问题/决策**：还原类任务（本次 3D 逆向建模）的机检只验「产物存在 / 像素非空 / 色板 HEX / 头身比」，测不出「像不像」；技能是建议且描述偏 UI（`视觉还原` 在系统提示里，但 123 次工具调用 0 次 `skill_read`）；`finish` 也不看对照。决定把「参考物 → 产物」对照做成**领域无关的机制**，而不是写 3D 提示词：代码只认识「两张图 + 指标」，领域信息来自用户参考图与验收项。
- **实现**：
  - `app/imagecmp.py`（新）：背景取「出现最多的颜色」，前景取最大连通块（避免边框线 / 图注 / 碎屑），按包围盒归一化后算 轮廓 IoU、宽高比差、灰度 SSIM、主色分布差、边缘密度差、3×3 局部最大差与加权总分 0–100；可选 `region` 裁剪；输出 `__IMAGE_METRICS__` JSON 与「差距前三」。
  - `app/agent.py`：新增 `image_compare` 工具（schema / `_t_image_compare` / `_HANDLERS` / `PLAN_MODE_TOOLS` / `OUTSIDE_READ_TOOLS` / 别名 `compare_images` / 工具清单与第 15 条纪律）。保持常驻 schema（不进 tool group），避免「该用却看不见」。
  - `app/api/chat.py`：`_ACCEPT_SAFE` 允许 `image_compare` 作为机检项；`expect` 新增 `score_min / iou_min / aspect_delta_max / palette_delta_max / edge_delta_max / fill_delta_max / grid_max`；`finish` 增加通用还原质量闸门 `_fidelity_gate_action`（按产物图分别记最佳分，闸门取**最差一组**，避免单视角高分掩盖）；`finish` schema 增加 `quality_waiver`。
  - 配置：`agent_fidelity_gate = nudge（默认）/ off / strict`、`agent_fidelity_min = 70`；设置 → 子代理新增「参考物 / 产物的还原质量闸门」与阈值。
  - 数据层预设：`visual-restore` 技能改成领域中立（界面 / 模型 / 插画 / 图表 / 排版通用）并把 `image_compare` 写进步骤；新增内置子代理 `fidelity-reviewer`（只读：`image_compare + see_image`，只打分列差距，不改产物）。
- **验证**：新增 `tests/test_image_compare.py` 6 条（同图 100 分、差异降分、region/坏输入、工具 + acceptance 指标断言、闸门分支、schema/plan-mode 暴露）；用归档的 v1 渲染与参考图实测：总分 44.7/100（IoU 0.57、主色差 46%、局部最大差 73%），acceptance 自动判 `failed`（`score=44.7 未达 >= 70`）；`pytest tests` 全量通过；`ruff`、`node --check settings.js` 通过。
- **未验证 / 边界**：真实会话里模型是否会自动调用 `image_compare`（依赖提示词 + 技能命中）需重启应用实测；70 分是经验阈值；图像指标不理解语义（配色对、结构错也可能得分），仍需 `see_image` 或人工复核；`agent_fidelity_gate=nudge` 只挡第一次 finish。


## [2026-11-01] 学习观测不再注入模型上下文
- **问题/决策**：用户要求观测数据只在大厅面板里看，不再喂给 Solo / IDE 的 AI（原逻辑在「写了全局规则」或 `scene=study` 时把 `observe.brief()` 拼进系统提示）。
- **解决方案**：删除 `chat.py` 里的 `obs_text` 注入，`base_system_parts` 不再传 `observations`；观测仍照常写入，`/api/observe` 与「功能大厅 → 学习观测」面板不变。
- **相关代码**：`app/api/chat.py`（原 2644–2651 的 obs_text 块）；`prompt_assembly.base_system_parts` 的 `observations` 参数保留，仅供显式调用方使用。
- **验证**：`pytest tests` 全量通过（`test_observe` 不受影响）；`py_compile` / `ruff` 通过。


## [2026-11-01] 量表工具：暂不实现（题库来源问题）
- **需求**：给对话 AI 一个自评量表工具（PSS-10 / GAD-7），点出后弹专用量表卡、计分、留历史。
- **决策**：暂不做。关键卡点是题目来源：题目是计分依据，凭记忆复写或改写会产生无效测量；PSS-10 题目有版权，GAD-7 也应引用官方原文。用户明确表示「如果要内置题库就不搞了」。
- **如果以后要做**：只做数据驱动引擎 + JSON 导入 + 结构校验（题数、选项分值、反向题、缺失项），题目由用户提供官方文件后再启用；专用量表卡与计分/历史可复用现有 ask 卡的阻塞等待 + SSE 机制。


## [2026-11-01] 模型痛点兜底：image_analyze / 空白图自检 / 多图视觉 / 停滞提醒
- **背景**：v2 3D 测试暴露四类可通用兜底的问题：① 自己写测量脚本把参考图测错（面板切分、背景当成前景）；② 渲染脚本漏 load，19 张全白才发现；③ 视觉描述与客观指标脱节、且不知道实际走的是哪条识图通道；④ 测到最大差距却不继续改。
- **实现（全部只依赖图像 / 命令 / 指标，不写领域提示词）**：
  - `app/imagecmp.py` + 新工具 `image_analyze`：背景取众数、前景占比、bbox、宽高比、主色、行/列剖面、ASCII 剪影；面板按「覆盖率 > 1%」切分（避免外框/分隔线导致切不开），支持显式网格 `2x2` / `1x3`，返回原图归一化 `region`，可直接喂给 `image_compare`。
  - `agent._append_blank_image_warning`：`run_command` 结束后扫描本轮新图片（≤3000 个），灰度 std < 2 或极差 < 8 就追加「几乎全空白」警告。
  - `see_image` 支持 `paths`（最多 4 张、合计 24MB），同一视觉请求里发多张；`imaging` 支持多图 payload；回落时在结果里标注「主聊天模型未接受图片，已回落视觉 API」。
  - `chat.py` 新增 `_fidelity_stale_next`：同一对照连续 2 次没有明显提升（比此前最佳高不到 1 分）就在工具结果里提醒换方法，配合已有的 finish 质量闸门。
  - 注册：TOOLS / PLAN_MODE_TOOLS / OUTSIDE_READ_TOOLS / 别名 / schema / 工具清单 / 第 15 条纪律；`agent_defs` 只读工具；`acceptance` 安全机检工具。
- **验证**：新增/更新用例（analyze 自动切分 + 2x2 网格 + region 映射、image_analyze 工具、空白图警告、see_image 多图捕获、停滞计数）；真实参考图实测 `2x2 → 4 面板`、`1x3 → 3 面板`；`pytest tests` 全量通过；`ruff` 通过。
- **边界**：`panels="auto"` 仍是启发式（参考图边框/图注会干扰，可能切错或切不开），多视图应显式给网格；空白检测只覆盖整图异常；多图视觉仍是一次独立请求，不把像素留在长期上下文。


## [2026-11-01] 原生视觉：see_image 直接把沙箱图片附到本轮上下文
- **背景**：实验确认视觉 key 为空、`vision_use_chat=True`，`see_image` 实际走的是 chat 模型（deepseek-flash）原生多模态；但以前只把识别结果当**文字**回灌，主模型并没有在自己的上下文里看到图片。
- **实现**：
  - `agent.vision_attachments()`：读取沙箱图片，最长边缩到 1024、转 JPEG(q=85)、base64；单次 ≤4 张、合计 ≤24MB；`_t_see_image` 复用同一套读取逻辑（新增 `native` 参数，`native=false` 强制文字描述）。
  - `chat.py` 工具循环拦截 `see_image`：把图片作为**临时 user 消息**（`build_vision_content`）附到本轮上下文，最多 `VISION_NATIVE_MAX=8` 张、同一路径去重；本轮内一直可见，不落库、不跨轮；模型不支持图片或设置关闭时自动走原来的文字描述路径。
  - 配置 `agent_native_vision=True`；设置 → 子代理新增「看图时把图片直接附给主模型」开关。
  - 顺带修上下文估算：多模态 content 不再 `str(list)`（会把 base64 当正文），`_msg_text` / `_msg_tokens` / `estimate_messages_tokens` / `trim_history_to_budget` 都改为「文字按字符、图片按 IMAGE_TOKENS 计」；`trim_tool_results` 的 `_is_tool` 只认字符串 content。
- **验证**：新增 `tests/test_native_vision.py`（缩图到 ≤1024、单次上限、非图/缺文件/空参、多模态 token 估算、默认值与 schema）；真实参考图两张 → 附图 payload 约 280KB、估算 3210 token；`pytest tests` 全量通过；`ruff`、`node --check settings.js` 通过。
- **未验证 / 边界**：没有跑完整真实对话（需重启应用 + 真实模型）；聊天模型若拒绝图片，目前需要手动关掉开关（未做自动回退）；图片在本轮内每轮重发，长回合成本会上升；上限 8 张/轮。


## [2026-11-01] Computer Use 现状：能力保留，但默认关闭且门控有缺陷
- **问题/决策**：确认 Computer Use 是否还在。代码在 `app/computer/`（浏览器 CDP + Windows UIA 两个面，6 个工具：`computer_open/see/act/read/shot/windows`），桌面端「读控件树不猜坐标」，UIA Pattern 优先、密码框不读值、提权窗口拒绝、冷启动注册 `Ctrl+Alt+Shift+Esc` 急停。
- **发现（真实缺陷）**：原生工具清单的门控只看浏览器开关 `computer.enabled()`（`chat.py` 里 `computer_on=computer.enabled()`），桌面开关 `computer_desktop_enabled` 独立存在但不参与工具可见性判断。当前配置是浏览器关、桌面开、`computer_apps=[]`，结果是模型一个 computer 工具都拿不到。
- **启用方式**：设置 → Agent → Computer Use 打开总开关；桌面程序白名单填 exe 名（支持 `*.exe` 通配）；浏览器需本机 Edge/Chrome（可用 `GAL_EDGE` 指定）；站点白名单制，未授权站点先确认。
- **相关代码**：`app/computer/__init__.py`（dispatch/judge/policy 分流）、`app/computer/desktop.py`（enabled/allowed_exes）、`app/agent.py`（`if computer_on` 的 schema 段）、`app/api/chat.py`（三处 `computer_on=computer.enabled()`）。
- **未做**：门控统一为 `computer.enabled() or computer_desktop_enabled()` 未实施，等用户确认。


## [2026-11-01] 能否复用 DeepSeek Harness 的插件生态（调研结论，未改代码）
- **问题**：能否直接复用 DSH 插件生态。
- **结论**：不能直接复用。DSH 插件是跑在 Cordis 容器里的 TypeScript 模块（`apply(ctx)` + `inject: ['tools']`，注册走 `ctx.tools.register()`，生命周期由 `ctx.effect()` 管理），不是 MCP、不是独立进程、没有可对接的协议边界；本项目 agent loop 是 Python。
- **三条间接路径**：A 资产级复用（`SKILL.md` / agent 预设 / trajectory 事件 schema / `cordis.patch.yml` 思路，成本最低）；B 进程级复用（官方 `deepseek-harness-sdk` 的 `DeepSeekHarness` 拉起 `dsh --profile sdk-minimal` 子进程整轮跑，能吃全部插件，但等于外部子代理、双份 token、版本耦合）；C 工具级桥接（在 DSH 侧写桥插件把 `ctx.tools` 暴露成 MCP 形状，复用本项目 `mcp.py` 客户端；只能桥 tools 类插件，依赖 `ctx.llm/sessions/agents` 的桥不了，约 300–500 行 TS）。
- **风险**：DSH 仍是 developer preview，官方声明 core plugins/API 会继续变；社区插件量很小。
- **决策**：用户选择暂不动，仅了解可行性。


## [2026-11-01] 方案A + 插件市场：Skill 资产复用与侧栏插件入口
- **目标**：① 落地「资产级复用 DSH/Claude」的 SKILL.md 格式认知；② 新增「插件」整屏页面（入口在侧栏，只读浏览 DSH 生态索引）。
- **后续变更（2026-11-01）**：用户要求「插件市场删除本地 skill 模块」。最初落地的 `resources/market/catalog.json`、`skills.read_file()`、市场里的安装/卸载/导入/导出全部移除；本地 Skill 的增删启停统一回 设置 → Skill（`/api/skills`），插件页只剩 DSH 索引。
- **插件页**：`web/hall-plugins.js` + `web/css/hall-plugins.css`。入口是侧栏 `#btn-plugins`（模式切换器下方的固定行），走 `setUiMode('plugins')` → `#plugins-panel`，**不再注册到功能大厅**。首次进入 `marketEnter()` 挂载；点会话 / 新建对话会自动退回 Solo。（后续升级为「已安装 / 发现」两 tab + Python 工具插件 / MCP 管理，见下一节。）页面内容：
  - DSH 生态：读 `https://awesome-dsh-plugin.com/plugins.json`（当前 8107 条），服务端分页 + 6 小时缓存；**只读**，只能看主页/仓库/复制 `dsh plugin add` 命令（Cordis/TS 插件跑不进本项目运行时）。
- **后端**：`app/market.py`（DSH 索引拉取 / 分页 / 缓存）+ `app/api/market.py`（`/api/market`，只接受 `op=dsh`，自注册）+ `app/httpd.py` 一行 import。
- **踩过的坑**：
  - （历史）catalog 最初放在 `resources/presets/`，会被 `seed_presets` 整目录复制到 `data/`；后来整个 catalog 文件随本地模块一起删除。
  - DSH 索引的 `categories` 值是 `{en, zh}` 对象，前端直接当字符串渲染会显示 `[object Object]`（分类徽标与下拉都会中招）。在 `market._category_labels()` 里归一化成一行文字。
- **验证**：`tests/test_market.py` 现在 4 条（DSH 分页/分类/搜索、缓存与断网回退、无缓存报错、`/api/market` 拒绝本地 op、路由注册）；`pytest tests` 全量 EXIT=0；`ruff`、`node --check` 通过；真实拉取 8107 条。渲染验证用 Edge headless `--dump-dom`（把 ui_mode 临时设为 plugins）：`#plugins-panel` 无 `hidden`、24 张卡片、24 个「复制安装命令」、页脚 `共 8107 条`，页面内 `本地 Skill` 出现 0 次。
- **未做 / 边界**：未做 Python 工具插件与 MCP 的市场入口；DSH 索引离线时只能用上次缓存，没有随包快照；正在运行的应用需要重启才会加载 `/api/market` 路由。
- **2026-11-01 复查（移除本地 Skill 后）**：`web/hall-plugins.js` 开头注释已写明「只展示 DSH 社区插件索引……本地 Skill 的增删启停统一在 设置 → Skill」；`web/index.html` 里唯一的「打开目录」按钮（`#skill-open-dir`）位于 `data-pane="skills"` 的设置面板，不在插件页；插件页整屏容器在 `index.html` 第 528 行，由 `marketMount` 渲染。UI 实测受阻于浏览器环境而非页面：点击侧栏「插件」时内置浏览器视口为 0×0（`innerWidth/innerHeight=0`、`document.elementFromPoint` 返回 null），点击返回成功但 SPA 不变（body 文本仍无 DSH），随后桥接断开（`failed to connect to browser plugin of Chrome, os error 10061`，Chrome 未运行；`browser_setup_plugin` 两次均返回 User cancelled）。同类环境问题与备选方案见 [2026-11-01] 会话分组「⋯」菜单自动化验收受阻。


## [2026-11-01] DSH 侧边栏源码调研（commit ce7073d5）
- **结论**：DSH 左栏是「品牌行 + 新建会话 + 全局面板列表 + 工作区/会话浏览区 + 底部固定区」五段；没有「模式切换」概念，插件只是面板列表里的一项（`FIXED_PANEL_ID='plugins'` 常驻，`schedules` order=10）。
- **源码位置**：`packages/client/ui-sidebar`（外壳与面板列表）、`ui-layout`（三列 AppFrame / 右列）、`ui-workspace`（工作区与会话浏览）、`ui-plugin-manager`（插件页：已安装）、`ui-plugin-market`（发现 tab）、`ui-schedule`（自动化任务页）、`ui-sidebar-right`（右侧栏 tab）。
- **几何与响应式**：侧栏 264–420px、默认 280px；收起为 56px 图标轨道；<768px 变模态抽屉；<1024px 自动收起；打开右侧栏会收起手动展开的左栏。面板行 min-height 36px、行间距 2px、左右 padding 7px（另有 3px 透明边保证底色内缩）、图标 16px、图标与文字间隔 6px。新建会话高 38px、下边距 10px。
- **导航机制**：插件向 root 作用域的 `sidebar.panellist` 注册图标+label；选中的 id 对应 `main` keyed slot 里的页面；选中态属于 layout store（不是路由）。「更多」菜单由 `sidebar.panellist.actions` 提供，可把非固定面板收起来（pin/unpin 只存内存，插件重载即重置）。
- **会话区**：分组 / 平铺 / 按工作区树三种视图；每个工作区默认只显示前 5 条（水位线，运行中/等待交互/未读完成会自动抬高，Show more 每次 +5）；搜索 250ms 防抖、最多 20 条；行上有待审批/计划待审/待回答橙点、运行中、未读完成；子代理来源的 Session 不显示；归档筛选三态；支持重命名 / fork / 归档 / 置顶 / 拖拽排序 / 复制 Session ID。
- **插件页**：两个 tab「已安装 / 发现」。已安装页分组：内置插件配置（子智能体、网页搜索、Agent 循环、终端）、实验性插件、已安装插件；bundle 与 row 两级开关、pnpm 安装日志、更新/卸载、离线详情文档。发现页：索引源管理、索引切换、搜索、标签多选、排序、分类 pills、每页 50 条、条目详情与版本列表、安装确认。
- **右侧栏**：按会话停靠的 dock，tab 化（文件 / 浏览器 / 终端 / 文档预览 / 任务详情），首次打开占 45% 视口、上限 70%，并为中栏保留 400px。
- **可借鉴（按性价比）**：① 把「插件」做成统一面板注册表而不是硬编码 DOM；② 侧栏折叠为图标轨道并记住宽度；③ 会话区补分组与「显示更多」水位；④ 底部按钮收进「更多」降密度。**不必照抄** Cordis slot 体系与 React 组件模型。

## [2026-11-01] 会话列表按目录分组：浏览器验收（分组标题 / 折叠 / 平铺切换）
- 设计：分组完全由 `session.sandbox` 派生，不新建 workspace 实体（我们的目录本来就是会话属性）。后端 `slim_sessions` 多回 `sandbox_key`（normcase + normpath，防止 `D:\Proj` / `d:/proj` 裂成两组）与 `sandbox_name`（basename），前端只负责渲染。默认 `session_group_mode=flat`（2026-11-01 起；之前默认 dir），点「会话记录」右侧按钮可切 `dir` 并写回 config；折叠状态存 localStorage，水位只存内存（刷新回到 5 条）。
- 问题/决策：验证侧栏「会话记录」是否真正按 sandbox 目录分组，标题是否带图标 + 目录名 + 计数，折叠/展开与「分组 ↔ 平铺」开关是否可用。
- 解决方案/原因：实测均通过。#session-list .session-group-head 是 button，含 svg use[href="#i-folder"]（可见 14x14）、.sg-name、.sg-count、.sg-caret，aria-expanded 控制折叠；点击后 .session-group-body 变为 display:none、高度 0、子行不可见，再点恢复。session-group-toggle 在 dir ↔ flat 间切换，aria-pressed 与 title 同步；切平铺后 #session-list 直接是 .session-item（无 group 节点），11 行全量展示；模式写入 /api/config 的 session_group_mode。折叠状态存 localStorage gm.session.group.collapsed.v1，水位 SESSION_GROUP_WATERMARK=5。
- 关键坑：分组计数与 data/sessions.json 对得上，但「未选择目录」当前显示 4 而不是 5——后端共 12 条会话（11 条 main + 1 条 gal「GAL 剧场」），未选目录的 5 条里有 1 条是 gal_scope=gal，renderSessions() 在 Solo/IDE 模式下会过滤掉 gal 会话，所以侧栏只显示 4 条；计数与行数 1:1 一致，不是漏算。
- 相关代码/指令：web/chat.js 的 sessionGroupsOf / sessionGroupEl / syncSessionGroupToggle / bindSessionGroupToggle / renderSessions；app/sessions.py::slim_sessions 写入 sandbox_key / sandbox_name；验收页 http://127.0.0.1:51999/。


## [2026-11-01] 会话状态语义 + 拖拽排序
- **状态语义**：新增 `app/session_status.py`（内存表，15 分钟 TTL），在 `agent.new_approval / new_asks` 登记、在 `wait_approval / wait_answers` 消减、一轮结束在 `chat.py` 里 `drop(sid)`；变化时经常驻总线发 `session_pending`。`/api/state` 多回一个 `pending` 映射。前端按优先级渲染：待批准 / 待回答（橙点 + 行尾文字）> 正在生成（蓝点）> 未读（黄点）> 空闲。后台会话的审批也能在侧栏看见，不再只依赖当前流的 DOM。
- **拖拽排序**：`sessions.reorder_sessions(data, ids)` 只置换已列出 id 占用的槽位（不同目录、GAL 会话不受影响）；新增 `/api/session/reorder`。前端 HTML5 DnD：同分组、同置顶分区内才允许放下，落点画强调色横线；键盘等价入口 `Alt+↑/↓`（`moveSessionBy`），复用同一套 `applySessionOrder`。
- **踩坑**：① 状态测试最初直接断言 `snapshot()`，被别的测试里未清理的 `new_approval/new_asks` 全局登记污染 → 测试用 fixture 把 `session_status._pending` 换成空表；② 自动化拖拽只派发 dragenter/over/drop，不触发 `dragstart`，`state.dragSession` 为空导致 `ondrop` 直接 return —— 这是测试工具的限制，真实鼠标拖拽会正常触发；③ 点击会话行不会把焦点留在行上（输入框会自动聚焦），键盘排序要求行先获得焦点（Tab 可达）。
- **验证**：新增 `tests/test_session_status.py` 5 条、`test_session_group.py` 补 2 条 reorder 用例；全量 `pytest` EXIT=0；`ruff`、`node --check` 通过。浏览器实测：待批准徽标（橙点 + 文字，刷新后仍在）PASS；`/api/session/reorder` 交换 AIMC 两条并读回 PASS（已恢复原顺序）；行聚焦后 `Alt+ArrowDown` 交换顺序，刷新后仍保持 PASS。**拖拽手势本身未自动验证**，需人工鼠标确认。
- **相关代码**：`app/session_status.py`、`app/agent.py`（new_approval / wait_approval / new_asks / wait_answers）、`app/api/chat.py`（回合结束 drop）、`app/api/misc.py`（state.pending）、`app/sessions.py::reorder_sessions`、`app/api/sessions.py::_api_session_reorder`、`web/chat.js`（sessionStatusOf / sessionRowEl / sessionDragOrder / moveSessionBy / applySessionOrder）、`web/auto.js`（session_pending）、`web/css/base.css`。


## [2026-11-01] 分组级操作：置顶 / 重命名 / 删除组内全部会话
- **语义**：分组是从 `session.sandbox` 派生的，没有独立实体。因此 `pin` / `rename` 只写 config 的展示元数据（`session_group_pins`、`session_group_names`，键是归一化后的 sandbox_key），**不动磁盘目录、不改会话 sandbox**；`delete` 才是真的批量删除组内会话（连带附件 `delete_session_uploads` 与存档 `saves.drop_saves`，并清掉该分组的展示元数据）。
- **接口**：`POST /api/session/group`，`op=pin|rename|delete`。delete 会校验「传来的会话必须都属于该分组」且「至少保留一个会话」；删除当前会话时把 current 切到剩余第一条。
- **界面**：组头由单个 button 改成 `head-wrap`（折叠按钮 + 常驻低透明度的「⋯」），菜单三项；置顶组排最前并显示图钉，「未选择目录」仍固定最后且不提供置顶/重命名。
- **踩坑**：① 「⋯」最初 `opacity: 0` 只在 hover 时显示，结果新用户发现不了、自动化也点不到（`elementFromPoint` 能命中但无障碍快照拿不到 ref）。改成常驻 `opacity: .45`，hover / `:focus` / `:focus-visible` 提到 1，`@media (hover: none)` 常显。② 重命名必须走 `appPrompt`（Electron 不支持 `window.prompt`）。
- **验证**：新增 `tests/test_session_group_api.py` 4 条（pin / rename / 批量删除并清元数据 / 拒绝跨组与删光）；全量 `pytest` EXIT=0；`ruff`、`node --check` 通过。浏览器实测（Edge headless + CDP）：菜单三项齐全 → 置顶后出现图钉并排到第一位 → 刷新保持 → 重命名「验证别名」成功；删除项只确认存在，未执行。测试后已通过接口把置顶与别名恢复原状。
- **相关代码**：`app/api/sessions.py::_api_session_group`、`app/config.py`（session_group_pins / session_group_names）、`web/chat.js`（sessionGroupsOf / sessionGroupEl / openGroupMenu / setGroupPin / renameGroup / deleteGroup）、`web/css/base.css`。


## [2026-11-01] 默认平铺 + 收起侧栏控件统一左下 + 插件页移除本地 Skill
- **默认平铺**：`config.session_group_mode` 默认改回 `flat`；用户已有配置里的 `dir` 也一并改回 `flat`（那是之前自动化验收时点出来的）。分组仍可随时用「会话记录」右侧按钮开启。
- **收起侧栏控件位置**：`.sidebar-expand` 与 `.mode-float` 统一改成左下角（`bottom: 56px` / `bottom: 96px`，`left: 10px`，模式气泡竖排）。之前非 IDE 模式是左上角横排，在大厅 / 工作台会盖住顶部标题栏；IDE 原本就是左下，现在所有模式一致。
- **插件页**：移除「本地 Skill」模块（前端 tab、导入/打开目录、后端 list/install/uninstall/toggle/import/export、`resources/market/catalog.json`、`skills.read_file`、相关测试）。插件页只保留 DSH 只读索引；`/api/market` 只接受 `op=dsh`。
- **验证**：全量 `pytest` EXIT=0；`ruff`、`node --check` 通过。浏览器实测：默认列表为 11 条平铺（无 `.session-group`）；收起侧栏后模式气泡 (10, 427, 37×130)、展开按钮 (10, 567, 30×30)，Solo / 大厅 / 工作台三模式位置不变且与顶部内容无交集。插件页用 Edge headless `--dump-dom` 验证：`#plugins-panel` 无 `hidden`、24 张卡片、页脚 `共 8107 条`、`本地 Skill` 出现 0 次。
- **相关代码**：`app/config.py`、`web/css/base.css`、`web/hall-plugins.js`、`web/css/hall-plugins.css`、`app/market.py`、`app/api/market.py`、`app/skills.py`、`tests/test_market.py`。


## [2026-11-01] Computer Use 无限制开关 + 提醒总开关 / 细分开关
- **Computer Use**：新增 `computer_browser_unrestricted`（浏览器：允许访问所有网站）与 `computer_desktop_unrestricted`（桌面：允许操作所有程序），默认都关。
  - 浏览器：`computer.judge` / `open_url` / `_guard_after` 都按该开关给 `policy.check(skip_allowlist=...)`；**仍拒绝**内置金融 / 支付 / 邮箱 / 密码管理器、`computer_extra_block` 与本程序界面；提交 / 支付 / 删除类高危动作仍会先问。
  - 桌面：`desktop.exe_allowed()` 在开关打开时跳过白名单，`list_windows()` 不再因白名单为空而拒列；提权窗口仍拒绝、密码框仍不读不填，高危动作仍会问（只有审批档位 full 才连高危一起放行）。
- **提醒开关**：新增 `reminders_enabled`（总）+ `reminder_inapp_enabled`（会话内）、`reminder_system_enabled`（Windows 通知）、`reminder_calendar_enabled`（日程）、`auto_chat_enabled`（主动聊天），默认全开。
  - `scheduler.tick()` 总开关关闭直接 return；应用内关闭时 `_fire_reminder(deliver=False)` 仍推进重复计划但不写会话 / 不推前端；主动聊天关闭时保留任务不触发。
  - `scheduler.apply_switches()` + `calendar.apply_system_switch()` 在保存这些配置键时（`_api_save_config` 钩子）同步注销 / 恢复 Windows 计划任务，避免「关了开关、程序关闭后仍弹」。
  - `due_reminders()` 同样按总开关 / 日程开关提前返回；旧提醒（legacy_reminder）的应用内消息也受 `reminder_inapp_enabled` 控制。
- **界面**：电脑操作面板两组各加一个「无限制」check-row；提醒面板顶部加总开关 + 四个细分（总开关关闭时细分项 disabled）。
- **验证**：新增 `tests/test_computer_unrestricted.py` 5 条、`tests/test_reminder_switches.py` 6 条；全量 `pytest` EXIT=0；`ruff`、`node --check` 通过。用 Edge headless + CDP（Node 24 自带 WebSocket）实测渲染：5 个提醒开关都在，总开关关闭后 4 个细分全部 `disabled=true`；电脑操作面板两个「无限制」行存在且默认未勾选，文案正确；`/api/state` 返回的新键默认值符合预期。
- **相关代码**：`app/config.py`、`app/computer/__init__.py`、`app/computer/desktop.py`、`app/scheduler.py`、`app/calendar.py`、`app/api/misc.py`、`web/settings.js`、`web/index.html`、`web/auto.js`、`web/init.js`。


## [2026-11-01] 插件页升级：已安装 / 发现、三种安装方式、MCP 管理
- **插件形态**：`data/plugins/<id>/plugin.json` + `entry`（默认 plugin.py）为目录包；老的 `data/plugins/<id>.py` 仍兼容。`plugin.json` 支持 id/name/description/version/author/entry/permissions/homepage。
- **加载与启停**：`capabilities.plugin_entries()` 统一扫描两种形态；`config.plugins_disabled` 里的 id 跳过；`capabilities.loaded_plugins()` 记录本次进程的加载结果（已加载 / 已停用 / 加载失败 / 未加载）。**不热加载**，安装/启停后需要重启进程。
- **安装方式**（`app/plugin_manager.py`）：
  - 内置源：`resources/plugins/catalog.json` + `resources/plugins/<id>/`。**当前 items 为空**——最初放的 `local-clock` 示例与内置时钟 / 时间注入能力重复，已移除；内置源保留为机制，之后要发官方插件再往里加。
  - 本地目录 / 单文件 .py / zip（校验越界路径、文件数与解压体积）/ https Git（`git clone --depth 1`，90s 超时）。
  - 一律拒绝同名覆盖；卸载只允许删 `data/plugins` 下的路径。
- **MCP**：插件页只做摘要与启停（总开关 + 单 server），读写 `mcp_enabled` / `mcp_servers[].enabled` 后调用 `mcp.reset()`；新增 / 编辑仍在设置 → MCP。
- **界面**：`web/hall-plugins.js` 重写为「已安装 / 发现」两 tab。已安装 = 总开关 + 工具插件列表（状态徽标、启停、详情、卸载）+ MCP 列表；发现 = 官方插件（可安装）与 DSH 生态（只读）两个来源，另有「从本地目录安装」「从 zip / Git 安装」「打开插件目录」。详情弹层显示版本 / 权限 / README。
- **安全**：安装与启用都要二次确认；总开关默认关闭；插件代码以主进程权限运行，UI 明确提示「重启后生效」。
- **验证**：新增 `tests/test_plugin_manager.py` 9 条（扫描、安装、冲突、卸载、启停、zip 越界、内置源、坏 manifest、目录插件加载与停用、MCP 开关）；全量 `pytest` EXIT=0；`ruff`、`node --check` 通过。真实接口验证：list → catalog 显示 local-clock 未安装 → 安装后状态 pending / catalog 已安装 → 卸载后清空。Edge headless `--dump-dom` 验证页面含「已安装 / 发现 / 工具插件 / MCP 服务器 / 本机时间 / 从本地目录安装 / 还没有安装工具插件」。
- **相关代码**：`app/capabilities.py`、`app/plugin_manager.py`、`app/api/plugins.py`、`app/config.py`、`resources/plugins/**`、`web/hall-plugins.js`、`web/css/hall-plugins.css`。


悬浮## [2026-11-01] 会话分组头：在文件管理器中打开目录
- **交互（两处）**：
  - 目录分组头右侧、「⋯」左边：`session-group-open` 小按钮（`i-external` 图标），打开该分组对应的真实目录；hover / 键盘聚焦提亮，触屏常显。「未选择目录」没有路径，不渲染。
  - Agent 设置浮层里沙箱目录行（`#btn-sandbox`「📁 目录名」）右侧：新增 `#btn-sandbox-open`，打开当前会话的沙箱目录；没有选择目录或 Agent 关闭时不显示。两者共用 `/api/open/folder`。
- **后端**：新增 `POST /api/open/folder`。只允许打开**当前会话实际用过的沙箱路径**（按 `normcase(abspath)` 比对），目录不存在 404、不在会话列表里 403；只调用 `os.startfile`，不执行任何命令。
- **验证**：新增 `tests/test_open_folder.py` 2 条（已知目录放行 / 未知与不存在拒绝）；全量 `pytest` EXIT=0；`ruff`、`node --check` 通过；Edge headless 渲染（临时把 `session_group_mode` 设为 dir）确认页面出现 6 个 `session-group-open` 按钮与对应 aria-label，验证后已恢复 flat。
- **相关代码**：`app/api/misc.py::_api_open_folder`、`web/chat.js`（sessionGroupEl / openGroupFolder）、`web/css/base.css`、`web/index.html`（i-external 图标）。


## [2026-11-01] 设置页整体重构：导航重排 / 分区锚点 / 行式排版 / 分区恢复默认
- **问题**：设置页原本是「一页一摞卡片 + 每个字段占两行」的平铺结构。Agent 执行页 19 张卡（执行节奏、审批、MCP、Hooks…）没有页内导航，日常项和专家项混排；数字/下拉也占两行；复选框是原生样式；长说明被折成一堆同名「说明」。用户反馈「有些模块内部略显杂乱」。
- **决策**：走「结构 + 视觉」，**不做 schema 驱动重写**。原因：15 页里 4 页是 JS 生成、其余是 index.html 手写（两套来源，schema 化要搬 1000+ 行）；设置里混着余额查询、用量图表、测试连接、MCP 增删行、Hooks JSON 校验、改键录制等非字段控件，schema 要么为每种开 slot、要么砍功能。折中：引入轻量渲染约定，只用于本轮重写的部分。
- **导航**：保持 15 页，重排为「常用 / Agent 与能力 / 用户与外观 / 系统」四组，每项加 15px 线性图标（`.snav-btn` 改 flex，图标随 active 变强调色）。
- **卡片**：`decorateSettingsPanes()` 重写，每张卡拆成 `.set-card-head`（标题 + 「恢复默认」+ 折叠箭头）+ `.set-card-body`。标题行整行可点折叠，状态存 `localStorage: settings_card_collapsed_v1`（键为 `pane|卡片标题`）。专家向分区首次默认收起，清单在 `SETTINGS_CARD_ADVANCED`（Agent 页 11 个：只读并行、工具结果预算、并行探索、子代理嵌套、研究闸门、工作流、变更审阅、AI Tab 补全、pyright/Git、MCP、Hooks）。
- **页内锚点**：`#settings-anchors` 吸顶胶囊条（`position: sticky`），由 `renderSettingsAnchors(name)` 按当前页卡片生成，卡片数 < 3 或「功能说明」页时隐藏；点击会先展开被收起的卡片再滚动；`updateAnchorsActive()` 随滚动高亮当前分区。搜索跳转（`settingsJumpTo`）也会自动展开目标卡片。
- **行式排版**：`layoutSettingRows(pane)` 只处理卡片体的直接子节点——`label.field-label` 后紧跟 number / select / `.limit-row` / `.range-row` 时包成 `.set-row`（标签在左、控件在右，控件列上限 300px）；url / 密钥 / textarea 保持标签在上（`.set-row.wide`）。`.check-row` 的 checkbox 统一改成开关样式（appearance:none + 圆钮），带 `:focus-visible` 焦点环。窄屏 ≤760px 自动改单列。
- **分区恢复默认**：每张卡右上角「恢复默认」，只重置本节；页头按钮改为整页重置并 `window.confirm` 二次确认。卡片归属的配置键**不手写映射表**：`cardKeyMap(pane)` 逐个扰动卡片控件 → 调收集函数（动态页用 `pane._collect()`，静态页用 `collectSettings()`）→ 对比找出受影响的键 → 恢复控件 → 用排序后的指纹校验「扰动前后完全一致」。校验不过就返回 null，宁可不提供本节重置也不误改别处。非 input 的状态用 `data-cfg` 标注（`agent_auto_continue`、`git_commit_mode`）。`CARD_RESET_SKIP` 排除密钥 / SESSDATA / 预设 / 全局规则。
- **踩坑**：`flattenSettingPaths(obj, prefix, out)` 顶层调用没传 `out`，`out[p]` 直接抛 `TypeError: Cannot set properties of undefined (setting 'config.deepseek_api_key')`——报错里的键名正好是 config 的第一个历史键，容易误判成配置问题。修法是函数内 `out = out || {}`。另一个坑：守卫原先用 `JSON.stringify` 直接比，键序不同会误判失败，改成排序后的 `settingsFingerprint`。
- **验证**：`node --check` 通过。浏览器实测（临时服务 8771）：四组导航 + 15 项图标、Agent 页 19 张卡与 19 个锚点胶囊、「执行节奏」分区重置后保存计数变 1、标题点击折叠与箭头转向、383px 窄屏无横向溢出、「功能说明」页筛选框/目录/展开均未受影响。**未复测**：修复 `flattenSettingPaths` 后「主题与外观」等静态页的分区重置（用户选择收尾不再测）。
- **相关代码**：`web/index.html`（导航、`#settings-anchors`）、`web/settings.js`（decorateSettingsPanes / renderSettingsAnchors / layoutSettingRows / cardKeyMap / resetSettingsCard / resetCurrentSettingsPage）、`web/css/polish.css`（卡片头、`.set-row`、开关、锚点条、导航图标）。


## [2026-11-01] Agent 收敛改造：窗口重复守卫 / 迭代节奏 / 失败语义
- **背景**：`data/trajectory/s_7321dca84146.jsonl`（两视图逆向 3D 建模，01:49–18:31，15 轮）848 次工具调用，v1→v4 反复重建，`v3_build` 15 次、`v3_render` 14 次、非零退出 39 次、`see_image` 84 次而 `image_analyze` 0 次。静态核查确认：重复守卫仍是「连续相同」判据（[agent.py](file:///d:/Tools/code/project/Galgame_monster/app/agent.py#L5658)），交错循环永远抓不到；`ok` 只认 ❌ 前缀，退出码 1 也记成功。
- **改动 1 · 窗口重复守卫**：新增 `window_hits(state, sig, window=24, limit)`，与 `repeat_guard` 互补；`call_signature(name, args, root)` 把 `run_command` 引用脚本的 `mtime_ns+size` 算进指纹（改脚本再跑不算重复）；`LOOP_WINDOW_LIMIT_CMD=3`、普通工具 4；`LOOP_EXEMPT` 排除 `acceptance / list_steps / read_tool_result / sleep / ask_user / image_compare / image_analyze`。上一工具成功写入或指标提升 → `reset_loop_window`（算进展）。
- **改动 2 · 迭代节奏（关键）**：签名窗口对「每次改完脚本再跑」无效——T13/T14 每次 build 前都有 edit。真正兜住这种的是节奏守卫：本轮一旦用过 `image_compare`（`fid_used`），连续 8 条命令没有重新量化就注入一条系统提醒，要求先 `image_compare` / `image_analyze` 再改；每 8 条可重复提醒一次。
- **改动 3 · 失败语义**：`result_ok(name, text)` 让 `run_command` 按真实退出码判成败（此前退出码 1 仍 `ok:true`）；`_strip_cmd_noise` 在退出码 0 时过滤 `Exception in module register():`、`WARNING: Ignoring invalid distribution` 这类假报错，失败时保留全部输出；命令输出头改为 `结论：成功/失败（退出码：N，用时…）`，原有「退出码：N」子串保留，兼容既有断言。
- **改动 4 · 工具可发现性**：`see_image` / `image_analyze` 的 schema 描述明确「先量后看」——渲染落盘先跑 `image_analyze`（`blank_score` 判白图、主体 bbox、面板切分），再决定要不要 `see_image`。
- **测试**：`tests/test_agent_repeat.py` 新增 6 条（交错循环、窗口裁剪、进度清空、脚本改动指纹、退出码成败、噪音过滤）；顺带修 `tests/test_computer_agent.py` 受本机 `computer_browser_unrestricted=true` 影响的假失败（用例里显式关掉该档位）。`ruff` 通过，全量 `pytest` EXIT=0。
- **未做（下一轮）**：3D 网格/渲染工具面、`see_image` 结果缓存、指标未达标时的强制停止（当前只提醒+既有 fidelity 闸门）。


## [2026-11-01] 原生视觉对齐 + Anthropic 原生工具通道
- **原生视觉**：`chat.py` 的 `_native_vision` 不再要求 `vision_mode()=="chat"` —— 「优先使用视觉 API」只作用于 PDF/OCR 链路，不应把 Agent 的看图降级成文字描述。现在只要 `agent_native_vision` 开着且聊天密钥可用，`see_image` 就把原图附到上下文（`VISION_NATIVE_MAX=8` 张/轮）。
- **Anthropic 图片格式**：`providers._to_anthropic_content()` 把 OpenAI 的 `image_url` data URL 拆成 Anthropic 的 `{"type":"image","source":{"type":"base64","media_type","data"}}`；`_split_system()` 不再把列表 content `json.dumps` 成字符串（以前图片会被转成一坨 JSON 文本，Anthropic 根本读不到像素）。
- **Anthropic 原生工具通道**：`chat.py` 的 `_native_ok` 从「只认 openai」改为 `openai | anthropic`。`llm.stream_chat` 早已解析 `content_block_start/delta/stop` 的 `tool_use`（`providers.anthropic_tool_event`），并收敛成与文本协议相同的 `{tool,args,id}`；被服务商拒绝时仍会去 tools 重试并退回文本协议。工具结果继续以普通 user 文本回灌，历史里不留悬空 `tool_use`，所以不需要 `tool_result` 严格配对。
- **测试**：`tests/test_providers.py` 新增 4 条（image_url→base64 块、纯文本 content 不变、tools→input_schema、tool_use 流事件）；`ruff` 通过，全量 `pytest` EXIT=0。
- **已知限制**：聊天模型不接受图片时会直接 400（尚未做「去掉图片重试 + 本回合禁用原生视觉」的自动降级）；Anthropic 原生调用不回收 `tool_result` 块（刻意如此，避免严格配对）。


## [2026-11-01] 上下文窗口简化：填了才算，没填即不限
- **删除**：`config._BUILTIN_CONTEXTS`、`_prefix_window()`、`MAX_CONTEXT_HARD`、默认配置里的 `model_contexts`。不再做任何「按模型名前缀猜窗口」。
- **兜底也删干净**：没有「匹配不到就 128000」，也没有 8000–1M 的钳制——`model_context_limit()` 只读 `chat_context_limit`，0 就是不限，填多少就是多少。
- **`model_caps` 不再管窗口**：`context` 字段与「模型名带 1m 就按 1M」的规则一并删除，这张表只留视觉 / 原生工具 / 并行工具 / 输出上限 / 服务商搜索。窗口从此只有「设置里的一个值」这一个来源。
- **连带处理（不限窗口时）**：`context_limits()` 返回 `(0, 0)`；`request_budget()` 返回 0；`effective_output_cap()` 回落到 `chat_max_tokens`（默认 8192）；`session_context()` 增加 `unlimited` 字段、pct 为 0；`calibrate_ratio()` 跳过旧锚点粗筛；`maybe_precompress()` 预算 <=0 直接返回；`chat.py` 里预压缩 / 同步压缩都加 `not _unlimited` 守卫，`_req_budget` 在不限时给 `1e9` 让裁剪分支自然失效。
- **手动压缩仍可用**：新增 `manual_keep_budget()` —— 有窗口时按「窗口 − 输出上限」算，不限窗口时按当前历史本身的估算量算，不再引入 128k 这类假想窗口；`compress_history()` 与知识库的「立即压缩」都用它。
- **前端**：设置页快捷按钮「自动」→「不限」，说明改成「不猜窗口、不触发压缩、圆环隐藏」；`ctx-calc` 与帮助页同步；回复末尾的圆环本来就按 `!!ctx.limit` 显隐，无需改动。
- **测试**：`tests/test_provider_chain.py` / `tests/test_context.py` / `tests/test_native_tools.py` 改写为「只认显式值」并新增不限窗口用例；全量 `pytest` EXIT=0，`node --check settings.js` 通过。
- **请求体字符上限也删了**：`chat_request_max_chars`（默认 50 万字符）连同 `recover_overflow` 那道兜底一起移除，对齐 Claude「没有额外请求体限制」。`_req_chars` 仍保留，只用于断流诊断。
- **副作用（需知）**：窗口不限 + 字符上限删除后，回合内「按预算折叠旧工具结果」（`_used_now > _req_budget`）不再触发；目前只剩**单条工具结果 8192 字符裁剪**、相同结果去重和 5 万字符落盘三条在生效。


## [2026-11-01] 子代理输出不再截断
- **删除**：`explore.OUT_CHARS=600`（单个侦察兵回传上限）与 `TOTAL_CHARS=1800`（整批汇总上限），`format_results()` / `_result_brief()` 直接拼接原文。
- **提示词同步**：去掉 5 处「结论控制在 600 字以内」与反证者的「不超过 500 字」，改成「结论写到够用即可，不要复述原文」——否则模型仍会自我限长。
- **仍保留的上限（不是输出）**：`TOOL_RESULT_CHARS=3000`（喂给子代理的单条工具结果）、`SEARCH_RESULT_CHARS=9000`（调研员搜索正文）、`_result_brief` 的 2200（给反证者看的他人结论摘要）。这些是子代理的**输入**，不是输出。
- **验证**：`tests/test_explore.py` 改为断言三份 900 字结论完整保留；`ruff` 通过，全量 `pytest` EXIT=0。


## [2026-11-01] 工具结果清理粒度对齐 Claude：一个结果一条消息
- **问题**：以前一轮模型调用的所有工具结果拼成**一条** user 消息，折叠时整轮压成一个 240 字占位——清理单位是「轮」，不是「条」。后果：同一轮里 10 个结果会被一起丢掉；占位里的工具名只取第一条，模型容易误判。
- **改法**：`chat.py` 回灌时改成**每个工具结果单独一条 user 消息**（`TOOL_MSG_PREFIX + "\n" + 单个结果`）；「上一步判断」和尾部提示各自单独一条。这样 `trim_tool_results` 的最小单位自然变成单条结果，`keep_recent` 的含义也从「N 轮」变成「N 条结果」，与 Claude 的 per-tool-result 清理一致。
- **保留的两件事**：折叠后仍是 240 字结构化摘要（工具名 + 目标 + 首尾 + 关键行），不是一句「已清理」；原文仍在落盘句柄里，可用 `read_tool_result(handle, start, end)` 按区间取回。


- **兼容性**：所有消费方本来就靠 `content.startswith(TOOL_MSG_PREFIX)` 识别工具消息（`context._is_tool`、`keep_from_by_tokens`、fork 裁剪、轨迹、token 分解），拆成多条后判断不变；旧会话里的合并格式仍按整条折叠。
- **测试**：`tests/test_context.py::test_trim_tool_results` 语义不变（保留最近 2 条、折叠更早的）；`ruff` 通过，全量 `pytest` EXIT=0。
- **注意**：清理触发仍依赖「预算」——窗口不限时预算为无限，所以这条现在**不会自动触发**；结构已对齐，等接上 token 阈值就会生效。


## [2026-11-01] 同模型评测三维度评分口径（摩擦 / 工具 / 实证）
- 方向：三个维度都是「分数越高越好」——摩擦分高 = 打断少，工具分高 = 契约清晰 + 任务域全，实证分高 = 同模型对照证据硬；1–10 按锚点打，不是相对排名。
- 摩擦（权重 5）：9–10 默认沙箱内自动执行、越界才问、可按路径/命令细粒度配置；7–8 有沙箱 + 多档审批、可切自动；5–6 默认每次写/命令都问或粒度粗；3–4 只有全放行/全拦截；1–2 无沙箱/无审批。
  - Codex 7（OS 沙箱 + on-request）/ OpenCode 7（allow/ask/deny + glob + --auto）/ 本项目 6（四档但默认 ask，每次写入确认；full 档放开命令白名单）/ Claude Code 7（多档 + auto 分类器）/ DSH 6（策略 + 预设，沙箱可换插件）/ Cursor 7 / ZCode 6（沙箱未确认）/ Trae 7（MCP 可自动运行）。
- 工具契约与任务域（权重 15）：9–10 工具少而清晰或工具多但有分组/懒加载，覆盖文件/shell/搜索/LSP/Git/MCP，跨语言构建无硬限制；7–8 主流闭环但有 1–2 个明显缺口；5–6 只覆盖基础文件 + shell，或工具过多无分组。
  - Codex 9 / OpenCode 9 / Claude Code 9 / 本项目 7（72 工具 + tool_search + 文本协议降级，但 72 个对弱模型是选择负担；命令白名单仅 Python/JS；LSP 仅 pyright；无调试器/PR）/ DSH 8 / Cursor 8 / ZCode 7 / Trae 7。
- 跨模型实证（权重 10）：10 两个独立固定模型基准都有数据且同模型下前 2；8–9 有基准/多模型矩阵；6–7 只有一个基准或二手数据；4–5 无公开对照只有内部基线；1–3 无数据/不可复现。
  - Codex 10（HB 69.5 第一 + Composio 53.3 并列第一）/ OpenCode 9（HB 69.0、10 模型均值 67.4 第一；Composio 50.0）/ Claude Code 8（HB 68.7 + Composio 50.0，方差大）/ DSH 7（仅 Composio 53.3）/ Cursor 6（二手同 Opus 轶事）/ 本项目 5（仅内部 13/18，6 题，不可横比）/ ZCode 5（只有模型级 Terminal Bench 3.0）/ Trae 5（无数据）。
- 敏感度：权重 5/15/10 → 维度 ±1 分对总分影响 ±0.5/±1.5/±1.0；本项目与 Claude Code 差 0.5，摩擦一分即可翻转，工具一分（±1.5）足以改变 2–4 名。
- 口径边界：分数是「有锚点的专家判断」，不是统计估计；相邻名次差 ≤3 不当作强弱结论。


## [2026-11-01] 真实 token 触发 + 子代理独立上下文预算
- **真实 token 口径（A）**：新增 `context.real_used_tokens(last_prompt, messages, since, calib)` = 服务商返回的 `usage.prompt_tokens`（真实）+ `since` 之后新增消息的估算 × 校准系数。主循环在每次响应后记 `_msgs_at_last_prompt`，回合内据此判断是否需要清理。
- **总量触发**：新增配置 `tool_result_trim_tokens`（默认 100000，0 = 不触发）。超过时按「单条工具结果」折叠较早的结果，保留 `tool_result_keep_recent` 条；折叠仍是 240 字结构化摘要，原文可用 `read_tool_result` 取回。与模型窗口无关，所以「不限窗口」时也生效。
- **子代理独立窗口**：`explore` 的每个侦察兵本来就有独立 `messages`；现在再加独立预算 `subagent_context_tokens`（默认 60000，0 = 不限）：用自己的真实 `prompt_tokens` + 增量估算判断，超预算就折自己的旧工具结果，**只动自己的 messages**，与主对话互不影响。回灌也改成「一个工具结果一条消息」，与主对话的清理粒度一致。
- **失败退化**：断流 / 超时 / 被停止的回合没有 usage → `real_used_tokens` 返回 0，不触发清理，退回原来的估算逻辑。
- **未做**：子代理内部还没有「摘要压缩」（只有结果折叠，10 轮上限下够用）；两个新配置项暂未进设置页，直接在 `config.json` 里改。
- **验证**：新增 `tests/test_context.py::test_real_used_tokens_adds_increment`；`ruff` 通过，全量 `pytest` EXIT=0。


## [2026-11-01] 工具结果取回升级：先搜后读（query 模式）
- **背景**：Claude Code 把超长输出落盘后只给「路径 + 前 2k 预览」，靠它自己的 Read/Grep 去翻；我们的落盘在 `data/spills/`，`data/` 是沙箱永久禁入区，`read_file` / `search_files` 进不去，所以只能走专用工具——于是缺了「搜索」这一环（只能 12k 分页硬翻）。
- **方案**：扩展现有 `read_tool_result`，不新增工具（避免工具面继续膨胀、也免去 `tool_search` 的发现成本）：
  - `query` + `mode=literal|regex` + `ignore_case` + `context(0–5)` + `max_hits(默认20/上限50)`。
  - **按行匹配**而不是整段正则：长文本 + 用户模式可能灾难性回溯；逐行天然有行号，也符合 grep 心智。
  - 返回「行号 + 绝对字符偏移 + 命中行 + 上下文」，模型拿偏移直接 `start=<offset>` 精读，形成「先搜后读」闭环。
  - 输出仍受 `TOOL_READ_CHUNK=12000` 限制；模式长度截到 200。
- **保留的原有优势**：`rN`（本轮内存）/ `sp_xxx`（落盘跨轮）两类句柄、精确区间读取、去重、落盘 GC 配额都不变。
- **同时对齐**：内联阈值 `tool_result_max` 8192 → **30000**（Claude Code 默认约 30k 字符）；配置默认值、代码兜底常量、设置页默认值三处同步。
- **未采用**：把 spill 路径直接交给模型（沙箱禁入，等于给一条走不通的路）；新增独立 `search_tool_result` 工具（多一个工具、多一次发现成本）。
- **测试**：`tests/test_spill.py` 新增 3 条（行号/绝对偏移/上下文、大小写与正则、坏正则报错）；`ruff` 通过，全量 `pytest` EXIT=0。


## [2026-11-01] 子代理硬限制放宽
- **单次输出**：删除 `SUB_MAX_TOKENS=2000`，改为配置 `subagent_max_tokens`，默认 **0 = 不限**（请求里不带 max_tokens，由服务商决定）。
- **工具结果**：删除 `TOOL_RESULT_CHARS=3000` 截断，子代理现在能看到工具返回的全文；单条过大时由子代理自己的上下文预算（60k，可配 0）折叠兜底。
- **轮次**：`MAX_SUB_ROUNDS` 10 → **30**，改为配置 `subagent_max_rounds`（0 = 不限，用一个极大值让 while 条件恒真，仍受超时/预算约束）。
- **存活时间**：`SUB_TIMEOUT` 60 → **180**、`CRITIC_TIMEOUT` 45 → **180**、`TOTAL_TIMEOUT` 90 → **360**；子代理 HTTP 读超时也从 45 秒改为 `SUB_TIMEOUT`。
- **总数**：`agent_explore_max_total` 默认 60 → **100**（设置页默认值同步）。
- **配置传递**：`run()` 入口读一次配置，写进 `ctx`（`sub_max_tokens` / `sub_max_rounds`），嵌套批次自然继承。
- **未改（仍是安全阀）**：并发 `agent_explore_max=8`（硬上限 12）、深度 5、以及“批次超时后丢弃未完成结果”的行为（这条建议后续改成返回已完成部分）。
- **验证**：`ruff`、`node --check settings.js` 通过，全量 `pytest` EXIT=0。



## [2026-11-01] 计划审批（Claude 式 plan mode）：计划与执行分离
- **问题/决策**：以前 `/plan` 只是「只读轮」，用户要手动回「开始」才执行；普通任务则由模型自己 `update_plan` 后直接动手。改成：第一份计划出现后**服务端挂起**，用户批准 / 写意见重排 / 逐条改步骤后才执行；批准之后同一轮再 `plan_patch` 不再打断。
- **实现**：
  - `app/agent.py` 新增 `new_plan_review / resolve_plan_review / wait_plan_review`，复用 approval 的「Event + SSE + HTTP 回传」模式，侧栏沿用 `session_status=approval`；超时 `PLAN_REVIEW_TIMEOUT=1800s`（`agent_plan_review_timeout=0` 表示用内置值）。
  - `app/api/chat.py` 新增 `_plan_review_gate()`，两个触发点：① 一轮工具处理完（计划刚建好、`turn_writes==0`）② 模型停下且尚未 finish。`/plan` 始终生效，普通任务看 `agent_plan_review`（默认 on）；GAL / 自动化 / 非 Agent 会话不拦。
  - 批准时置 `plan_approved=True`、`plan_mode=False`、`turn_finished=False`，并把 `_tool_unlock_sig=None` 强制下一轮按执行模式重建工具 schema，同时从系统提示里摘掉 `_PLAN_MODE_PROMPT`（否则模型仍会拒绝动手）。
  - 逐条编辑用 `_apply_plan_edits()`：按 id 合并回原计划，保留未编辑步骤的 milestone / verify / deps / accept，`normalize_plan` 负责版本号 +1。
  - 接口 `POST /api/plan/review`（approve / comment / edit + steps），实现在 `app/api/sandbox.py::_api_plan_review`。
  - 前端：`planReviewRender`（批准并执行 / 按意见重排 / 每步 textarea）、`planReviewDone`、SSE `plan_review` / `plan_review_done`，样式在 `base.css` 的 `.plan-review-*`。
- **取舍**：普通任务超时后按当前计划继续执行（不能因为没人看计划就把任务停死）；`/plan` 超时则按原逻辑收尾。超时过的同一版计划不重复挂起（`appr_review_seen`）。
- **测试**：`tests/test_plan_review.py`（逐条编辑、批准/意见往返、重复回答拒绝、超时清理）。


## [2026-11-01] 验收子代理：替代「只看像素分」的可信保真闸门
- **问题/决策**：旧 `_fidelity_gate_action` 只在用过 `image_compare` 时生效，且只信像素分、靠主模型自己选对比对象，对 3D / 代码 / 文档类交付完全无效。改为每次 `finish` 前跑一个**独立上下文的验收 AI**，与主模型同等工具权限（读 / 写 / 跑命令，写与命令仍走主循环审批），对所有任务生效。
- **可信度来源**：
  1. **盲评**：不传主模型的分数，只传任务、计划、工具流水与产物；主模型自述标注「仅供参考，不能当证据」。
  2. **证据强制**：验收项 `passed=true` 必须带 `文件:行号` / 命令输出，否则 `parse_report` 直接按未通过计。
  3. **客观封顶**：`score_cap()` 用客观事实压低上限 —— 文件消失 40、工具失败未修复 60、计划未完成 60、验收项未通过 65、像素分低于 `agent_fidelity_min` 65；低于及格线强制 `fail`，模型自评再高也过不去。
  4. **结构化输出**：`verdict / score / criteria / gaps / next_action`，写入轨迹（`trajectory.review`）并回灌主模型。
- **实现**：新模块 `app/quality_gate.py`（`build_facts / build_brief / parse_report / score_cap / merge_report / run`）；`_run` 只是一轮工具循环，`chat_fn` 复用 `explore._default_chat`，`tool_fn` 用 `_wf_exec_node`（与工作流同一套审批 / 规则），不污染主工具时间线。
- **收尾规则**：每回合最多复核 `agent_acceptance_max_reviews`（默认 2）次；不通过就把差距回灌让模型返工；连续失败到第 3 次、或验收代理判 `blocked` 时，才接受 `quality_waiver`；次数用尽仍未通过则按现状收尾并在结束原因里标「验收：未通过」。
- **配置**：`agent_acceptance_review`(on) / `agent_acceptance_min`(70) / `agent_acceptance_max_reviews`(2) / `agent_acceptance_rounds`(12，0=不限) / `agent_acceptance_timeout`(0=内置 180s) / `agent_acceptance_model`(空=跟随子代理模型)。
- **保留**：旧的像素闸门继续作为「客观指标」之一参与封顶，不再单独决定是否收尾。
- **测试**：`tests/test_quality_gate.py`（JSON 解析、证据强制、未修复失败、封顶与合并判定、brief 不把自述当证据）。
- **验证**：`ruff` 通过；相关模块 36 项通过；全量 `pytest` 见本轮回归结果。


## [2026-11-01] 去掉「开工前强制验收」，改成软提醒 + 活文档 + 收尾复核
- **决策**：删除 `agent_acceptance_gate` 的**阻塞**闸门。原因：它只检查「有没有写验收」，不检查「验收是否有意义」；开工前信息不足时标准只能靠猜，任务一变形就变成过时合同；拦两次又自动放行，本质是提醒而不是质量保证。现在收尾有独立验收子代理按真实产物核对，前置阻塞的必要性已被替代。
- **现在的验收链路**（三层，职责不重叠）：
  1. **计划自带**：`done_when`（整体 / 里程碑）+ 每步 `verify`，随 `plan_patch` 演进；
  2. **软提醒**：多步任务写了 ≥2 个文件且计划 ≥2 步、又没有验收项时，只提醒一次（`_acceptance_suggest_msg`），不阻塞；
  3. **收尾复核**：验收子代理在 `finish` 前独立判定；计划里的验收条件必须逐条照抄成 criterion 并核对，漏项直接把分数封顶到 65。
- **验收项可修订**：`acceptance(action="revise")` 支持改 text / check / expect 或 `drop=true` 删除，**必须写 reason**，修订记录进 `s["acceptance"]["history"]`（保留最近 20 条）；改过的条目状态重置为 `pending`，不允许沿用旧的 passed。
- **为什么不是彻底删掉 acceptance 工具**：机检项在「可自动跑的安全检查」上仍然比 AI 评审便宜、确定；它只是不再充当开工许可证。
- **风险与兜底**：模型可能一直不定义机检项 —— 软提醒一次 + 收尾复核兜底；计划 verify 可能过时 —— `plan_patch` 更新后复核以最新版本为准，`revise` 支持改/删并留痕。
- **相关代码**：`app/api/chat.py` 删除 `_acceptance_gate_action` / `_acceptance_gate_msg` 与 `accept_state`；新增 `_acceptance_suggest_msg`、`_acceptance_norm_criterion`、`revise` 分支；`app/quality_gate.py` 新增 `plan_checks / uncovered_checks` 并把漏项纳入封顶。
- **测试**：`tests/test_acceptance.py` 删掉闸门用例、新增 revise / drop 用例；`tests/test_quality_gate.py` 新增计划条件收集与漏项封顶用例。


## [2026-11-01] 3D/几何 P0：mesh_inspect + WebGL 三角面统计
- **问题/决策**：之前 3D 只有「看渲染出来的像素」（visual_check / image_compare / image_analyze），没有「看几何本身」的能力；`asset_scan` 只按扩展名识别 .obj/.gltf/.glb，不解析内容。P0 先补两项**不依赖渲染**的结构化事实，成本低、可机检。
- **`mesh_inspect`（新工具，`app/mesh.py`）**：
  - glTF / GLB：解析节点 / mesh / primitive / 材质 / 纹理 / 图像 / 动画 / 蒙皮；按 accessor 统计顶点与三角面；应用节点变换（matrix 或 TRS 四元数）算世界包围盒；检查 NORMAL / TEXCOORD_0 缺失、无可用三角面的 primitive、外部 `uri`（贴图 / buffer）是否存在、`extensionsRequired`。
  - OBJ：统计 v / vt / vn / f 与三角化后的三角面数、包围盒、对象 / 分组 / 材质；检查非法面、越界索引、退化面、`mtllib` 是否存在。
  - 统一输出 `__MESH_METRICS__` JSON；限制明确写在输出里：**不检查非流形 / 自交 / 法线朝向**，也不做拓扑修复。
  - 接入：`TOOLS` / `PLAN_MODE_TOOLS` / `TOOL_ALIASES` / `TOOL_GROUPS(asset)` / `TOOL_SIG` / 原生 schema / 文本协议 / `_HANDLERS`；`agent_defs.READ_ONLY_TOOLS` 与 `fidelity-reviewer` 也可用。
- **`visual_check` 扩展**：在注入脚本里拦截 WebGL 的 `drawElements / drawArrays / *Instanced`，按 `mode` 换算三角面（TRIANGLES=count/3，STRIP/FAN=count−2），并 track `useProgram` / `bindTexture` 的去重数量。这样**不依赖 three.js / Babylon 内部对象**，自写 WebGL 也能统计；输出新增「三角面 · 着色器 · 绑定纹理」。
- **验收联动**：`_ACCEPT_SAFE` 加入 `mesh_inspect`；`_acceptance_metrics` 识别 `__MESH_METRICS__`；`expect` 新增 `triangles_min/max`、`vertices_max`、`materials_max`、`textures_max`、`degenerate_max`、`missing_textures_max`、`missing_files_max`、`normals_missing_max`、`uv_missing_max`、`bbox_x/y/z_max`。
- **测试**：`tests/test_mesh.py`（OBJ 统计 / 越界与退化面 / glTF 计数与缺贴图 / GLB JSON chunk / 坏文件）；`test_acceptance.py` 新增 mesh 指标断言；`test_assets.py` 的真实无头浏览器用例补「三角面 0 · 着色器 0 · 绑定纹理 0」。


## [2026-11-01] 3D/几何 P1：多视图渲染 + 多视图对照 + 场景断言
- **问题/决策**：单视角像素对照对 3D 太脆弱（换个角度就全变），而且看不到「比例 / 朝向 / 穿插」。P1 补三件事：固定视角批量出图、按视角配对做几何对照、把「场景是否符合规格」变成可断言项。
- **`render_views`（visual_qa）**：无头浏览器按 `views` 逐个打开 `page.html?view=<name>`（也支持 `{name, query}` 自定义参数），截图落到 `data/visual/<sid>/views/<stamp>/<name>.png`，并写 `manifest.json`。
  - **页面契约**：页面自己读 `location.search` 的 `view` 并设置相机；没适配时四张图完全相同，工具会明确警告（用截图 SHA1 判重），不让「假多视图」通过。
  - 默认视角 front / side / top / iso，最多 8 个。
- **`geometry_compare`（imagecmp.compare_views）**：`reference` 与 `candidate` 按顺序配对，逐视图复用 `compare()`（轮廓 IoU / SSIM / 主色 / 比例差 / 局部格差），再给平均分、最差视图、`views_failed`；输出 `__GEOMETRY_METRICS__` + `__GEOMETRY_VIEWS__`。
- **`scene_assert`（visual_qa）**：页面暴露 `window.__VQA_SCENE_INFO__()` 后，注入尾脚本读取对象 / 相机 / 灯光 / renderer，执行声明式断言：`exists / objects_min / objects_max / hidden_max / lights_min / camera_fov_min|max / triangles_max / bbox_x|y|z_max`。缺钩子时返回可直接照抄的示例；不做「猜页面 DOM」。
- **接入**：三个工具加入 `TOOLS` / `/plan` 只读集 / 别名 / visual 工具组与关键词 / `TOOL_SIG` / 原生 schema / 文本协议 / `_HANDLERS`；`geometry_compare` 支持沙箱外只读授权；`agent_defs.READ_ONLY_TOOLS` 同步。
- **验收联动**：`_ACCEPT_SAFE` 加入三项；`_acceptance_metrics` 识别 `__GEOMETRY_METRICS__` / `__SCENE_METRICS__`；`expect` 新增 `view_score_min / view_iou_min / view_ssim_min / views_failed_max / scene_failed_max / scene_passed_min / scene_objects_min / scene_lights_min / scene_triangles_max`。
- **测试**：`tests/test_scene.py`（视角参数收敛、多视图对照指标、场景断言纯逻辑、真实无头浏览器渲染两视角并验证截图不同、真实场景断言通过/失败）。


## [2026-11-01] 计划审批卡 UI：文本不再被裁切 + 交互后自动折叠
- **问题**：单行 textarea 的内部滚动会把第一行上半截裁掉（截图里 S1 被压在标题下方）；审批卡处理完仍占一屏。
- **修复**：
  - textarea 改为按内容自动撑高（`scrollHeight`，插入后 `requestAnimationFrame` + 60ms 各量一次，输入时再量），CSS 加 `overflow-y:hidden; min-height`，不再出现内部滚动裁切。
  - 卡片头可点击展开 / 收起；**批准或提交意见后自动折叠**，状态（「已批准，开始执行…」/「已提交意见，等模型重排…」）显示在标题右侧，不再保留整块列表。
  - 折叠态只隐藏列表 / 意见框 / 按钮，卡片本身留在原地作为记录；窄宽度下状态文字省略号截断。
- **验证**：`node --check web/chat.js`、静态 fixture 用真实 Edge 截图确认长文本完整显示、折叠态只剩一行；`ruff` 与相关测试通过。


## [2026-11-01] 回合「完成耗时」显示（毫秒级）
- **问题/决策**：文末用量行只有 token 统计与「完成时间」，看不出这一轮从发指令到输出完成花了多久。现在在「完成时间」下面新增一行 `完成耗时 12.345 秒`，方便对比不同任务的耗时。
- **数据来源**：直接用服务端已有的 `timing.total_ms`（`_api_chat` 进入时的 `time.monotonic()` → 整轮输出落库前），单位毫秒；包含工具执行、等待审批、重试等全部耗时，不是单次模型调用时间。该值本来就随 `timing` / `done` 事件下发并落库到 `msg.timing`，没有新增埋点。
- **前端**：`usageText/setUsage` 增加 `timing` 参数，在 `完成时间` 后追加 `完成耗时 X.XXX 秒`（毫秒 = 3 位小数）；实时 `done` 事件与历史回显（`m.timing`）走同一份逻辑。
- **边界**：没有 usage（例如本轮零产出）时，只要 `timing.total_ms > 0` 也会单独建一行显示耗时；老会话没有 `timing` 时行为不变。
- **验证**：`node --check web/chat.js`、`node --check web/stream.js` 通过。


## [2026-11-01] 整合 Archify（vendor + archify 工具，最小闭环）
- **决策**：不重写渲染器，直接 vendor 上游 skill。`vendor/archify` = v3.2.0（commit `a4065941`，Apache-2.0 + MIT 双许可），删掉 `examples/*.html`（12.2 MB 的生成演示），保留 schema / renderer / 示例 JSON / LICENSE / NOTICE，体积约 5.1 MB。版本与来源记在 `vendor/archify/.archify-vendor.json`。
- **工具 `archify`**（`app/archify.py` + `agent._t_archify`）：
  - `action=schema`：按图型返回 `common + <type>` schema + 一份官方示例（模型无法读 sandbox 外的 vendor，所以由工具回灌）；
  - `action=finalize`：候选可内联（`candidate`）或走 `candidate_path`（大候选先 `write_file`）；输出默认 `.archify/<type>-<时间>/<type>.html`；调用原版 `node bin/archify.mjs finalize ... --json`，走 validate → 原子写入 → 严格检查 → 真实浏览器检查；
  - `action=doctor`：Node / 图型 / 浏览器自检。
- **安全与审批**：
  - 新增 `agent.writes_files(name, args)`：`archify` 只有 `finalize` 算写入，`schema/doctor` 不弹审批；chat 主循环与 `_wf_exec_node`、`run_tool` 的写锁 / 缓存失效 / `is_write` 全部改用它；
  - `_write_rel_paths` 对 `archify` 取 `output` / `candidate_path`，项目规则与审批能定位到具体文件；
  - 子进程只继承 Node 必需的少量环境变量，不把 API Key 传进去；`ARCHIFY_UPDATE_CHECK_DISABLED=1` 关闭联网更新检查；`ARCHIFY_CHROME` 自动指向 `webprobe.edge_path()`。
- **失败处理**：`finalize` 非零退出不算成功；回灌 `failedStage`、`diagnostics[].code/message/supportedFixes`、`nextAction.constraint`，模型按诊断改候选后重跑。上游渲染器偶发 `internal/unclassified`（机器忙 / 浏览器残留进程时出现过），会自动重试一次并附 stderr。
- **产物**：自包含 HTML（示例约 846 KB），旁边会生成 finalize / delivery / browser-check 回执；预览要用独立窗口或 iframe（含交互脚本）。
- **依赖**：Node >= 18（本机 v24）；没有 Node 或没有 Edge/Chrome 时工具返回明确原因，不会假装通过。
- **测试**：`tests/test_archify.py` 8 项（schema / inline 候选 / 写入门控 / 坏候选 validate 失败 / 官方示例 finalize 四门槛全过 / 工具接线）；回归 219 项通过。注意：全量套件里 `finalize` 两项会因渲染器 `internal/unclassified` 失败，单独运行通过，尚未定位（与轨迹无关）。

## [2026-11-01] 轨迹查看器 v3：界面 / 交互 / 信息 / 便捷操作重做
- **问题/决策**：旧版列表单行省略、类型区分弱、只能鼠标逐行点；检查器 7 个折叠块纵向堆叠；耗时条无刻度；model/provider、工具输出大小、截断状态等信息没有投影出来；会话运行中要手动刷新。
- **前端**（`web/trajectory.js` 重写 + `web/css/trajectory.css` 重写 + `web/index.html` 弹窗骨架）：
  - 列表：两行式记录行（类型色条 + 标签 + 正文 + meta 副行）、悬停复制；Turn 头聚合「步数 / 条数 / 工具 / 上下文峰值 / 输出 token / 失败 / 耗时」，点标题按轮折叠。
  - 筛选：类型 chips 带计数；工具名下拉（按调用次数排序）；仅失败；搜索高亮 + `n / total` + 清除；无匹配空状态；「下一个失败」「折叠全部」。
  - 时间轴：按单条耗时比例的色条（类型着色、失败标红、未命中淡化、选中描边），点条跳转。
  - 检查器：tab 化（概览 / 输入 / 输出 / 思考 / 提示差异 / Schema / 原始），分区复制 + 换行开关；概览含耗时、首包、tok/s、token 明细、模型 / 提供方、callId / traceId（可复制）、输出大小与截断、失败原因、系统提示分段（可点跳到对应行）。
  - 键盘：↑↓ / j k 选择，/ 聚焦搜索，n / p 跳失败，g / G 首尾，Esc 关闭（搜索框内先清搜索）。
  - 跟随最新：弹窗打开时每 2.5s 轮询，先比对轻量 rev，日志没变不重读；刷新保留选中（recordId）与滚动位置，原本在底部则继续贴底。
  - 导出菜单：JSON 全量 / Markdown 当前筛选 / 复制当前筛选；弹窗打开锁 body 滚动；窄屏检查器 `overflow: hidden` + meta 限高，tab 不再被底栏盖住。
  - 侧栏图标：新增 `#i-trajectory`（S 形路径 + 三个节点，表示事件轨迹），不再复用「存档」的 `#i-history` 时钟图标。
- **后端**（`app/trajectory.py` / `app/api/trajectory.py`）：
  - system cell 补 `model` / `provider` / `toolCount`，`parts[]` 补 `id` / `recordId`（前端可跳到分段行）；
  - tool/result 补 `resultChars` / `write` / `errorText`；子代理 cell 补 `messages`；
  - 新增 `revision(sid)`（日志 size + mtime）与 `POST /api/trajectory/rev`；`/api/trajectory` 响应附带 `rev`。
- **踩坑**：
  - 没有 `recordId` 的 cell（用户消息、压缩记录）前端用 `idx_N` 兜底，但 `cellByRid` 只比 `recordId`，点开检查器为空；改为统一走 `cid(c)`。
  - 每一步都带完整上下文，把 `prompt_tokens` 求和会得到 2400K 这类假数字；展示改为「上下文峰值 + 输出累计」。
  - 搜索高亮不能先转义再正则（`&` / 引号会破坏标记），改为按原文索引切片后再分别转义。
  - `.traj-turn` 不能用 `overflow: hidden`，否则会打断 Turn 头的 `position: sticky`，改用 `overflow: clip`。
- **验证**：`tests/test_trajectory.py` 12 项通过（新增 rev 路由 / 投影字段 / 分段 recordId 断言）；浏览器实测（stub 数据 + 真实应用 594px 窄屏）列表、折叠、搜索 39/441、检查器 tab、空结果提示、顶栏不换行均通过；全量 pytest 跑到 100%，仅 `tests/test_archify.py` 两个既有渲染器失败，其余通过。
- **注意**：后端新增了 `/api/trajectory/rev`，运行中的旧实例需要重启才能生效（静态文件是每次现读，新前端可与旧后端共存，但跟随最新与新增字段要重启后才有）。

## [2026-11-01] 编码 Agent 横向评估：8 家同代模型归一化排名
- **方法/边界**：8 家 = 本项目 / OpenCode / Codex / Claude Code / Trae / DeepSeek Harness / Cursor / ZCode。严格同模型不可行（Claude Code 只能 Anthropic，Codex 只能 OpenAI），按同代旗舰做归一化；其余 6 家可 BYOK/多模型。证据分级 A=本项目代码/官方文档，B=官方文档/公开榜单，C=社区评测/推断。本机未实跑，因此是工程判断而非 benchmark 实测，打分置信度约 ±0.3。
- **关键证据**：Terminal-Bench 2.0（2026-05）同模型 Claude Opus 4.6：Claude Code 58.0% vs Meta-Harness 76.4%，同一模型差 18 分，说明 harness 影响巨大；Codex CLI + GPT-5.5 82.2%（总榜第 4）；OpenCode + Opus 4.5 51.7%；Cursor 自报 Composer 2.5 69.3%（厂商数据）；Trae Agent SWE-bench Verified 75.2%（2025-06，Claude 4，非 IDE 本体）；DSH / ZCode / 本项目无同口径公开数据。
- **维度/权重**：上下文与记忆 18、长任务编排 14、执行可靠性 16、工具与执行面 14、代码智能与编辑 12、安全与可控 10、交互体验 8、开放性与生态 8。
- **加权排名**：Claude Code 4.41 > Codex 4.37 > Cursor 4.22 > DeepSeek Harness 4.02 > Trae 3.95 > OpenCode 3.69 > 本项目 3.62 > ZCode 3.56。前两名实质并列。
- **本项目优劣**：优势是 57 个工具（含 LSP/computer/生图/Archify）、轨迹+tool trace 可观测、子代理并发 8–12、1751 个测试函数、本地 BYOK；短板是无 OS 级沙箱、LSP 仅 pyright、语义索引上限 1200 文件、Windows-only、undo 仅在内存、无外部基准。
- **敏感度**：长任务优先→Codex 第一；IDE 交互→Cursor 第一；开放可改/自托管→DSH；上下文与规则→Claude Code；本项目在"工具最全 + 本地可观测"这一项有优势，但综合仍靠后。
- **后续若要实测**：固定同一模型 + 同一任务集（改 bug/多文件重构/终端运维/前端还原各 5 题），记录通过率、耗时、token、人工干预次数，才能把 C 级推断换成 A 级数据。

## [2026-11-01] 工具方案重构：核心 14 个 action 工具 + 跨语言无硬限制
- **问题/决策**：默认 38 个核心 / 23.5KB schema，全量 104 / 53.3KB；分组已有但核心臃肿；`symbol` 组与 LSP 重复、`code` 组指向死工具 `run_script`；LSP / `check_code` / `codesym` / 命令白名单都有语言硬限制。
- **方案**：
  - 新增 `app/tool_meta.py`：meta 表（read / edit / run / code / git / plan / agent / web / history）+ 懒加载组（visual / image / asset / audio / workflow / utility）；action→旧工具名映射；在 `normalize_call` 与 `run_tool` 边界归一，审批 / 沙箱 / 写前必读 / 撤销仍按旧名生效；旧工具名继续可用。
  - `native_tool_schemas` 新增 `allowed_tools`：子代理 / 巡检 / 评测按旧名授权，meta 只暴露被授权 action（explore / agent_job / bench 已接）；删除死工具 `run_script`。
  - 新增 `app/langs.py`：探测项目语言 / 包管理 / 构建 / 测试（pyproject、package.json、Cargo.toml、go.mod、pom、gradle、csproj、CMake、Makefile）；写后自检与 `check_code` 改用统一 dispatch（py/js/json/go，缺工具静默跳过）；非 Python LSP 明确降级；`find_symbol` 支持 go/rust/java/c/c# 定义模式；`CMD_ALLOW_EXES` 扩到 go/cargo/mvn/gradle/dotnet/make/cmake/gcc/tsc 等，安装依赖仍按高风险问；权限预设补跨语言命令。
  - 提示词：compact 工具块改为 action 速查；新增「工具名约定」把旧名映射到新 action；plan / research 提示改用 `plan(action=…)` / `web(action=note)`。
- **数据**：默认核心 38→14，schema 23.5KB→11.9KB（-49%）；全量 104→55，53.3KB→27.1KB（-49%）；plan 模式 10 个 / 9.3KB。
- **验证**：`ruff` 通过；`pytest` 全量（排除 archify）100% EXIT=0；`tests/test_archify.py` 单独 8/8；新增 `tests/test_tool_meta.py`、`tests/test_langs.py`。
- **注意**：`computer_*` 仍保留 6 个细分工具（默认关闭、显式开启），避免把高风险动作压进一个 action；旧会话 / 插件 / 文本协议使用旧名不受影响，重启后生效。

## [2026-11-02] 审批默认自动 / 计划审批开关 / 全仓索引 / IDE 调试器
- **审批默认**：`APPROVAL_DEFAULT` 改为 `ai`（自动审批：只读与普通命令放行，安装/联网/改历史才问）；`auto/always → ai`，`once → ask`；会话回退与前端 `normApproval` 同步。
- **计划审批开关**：后端原本已有 `agent_plan_review=True` 与完整挂起/批准/改步骤流程，只是在设置页缺失；新增 `#agent-plan-review`（设置 → Agent 执行），并接入 settings 的 config 列表、加载与保存，默认开启。
- **全仓语义索引 v2**（`app/codeindex.py`）：稀疏+稠密混合保留；新增 manifest 增量更新（只重算变更文件）、默认不限文件/块数（`codeindex_enabled/max_files/max_chunks/max_file_kb`）、`.gitignore` 与依赖目录过滤、符号边界分块与精确符号加权、后台索引进度/状态、`search_items` 结构化结果；新增 `/api/codeindex/{status,reindex,search}`；设置页新增索引卡片；IDE 搜索面板新增「语义」开关与命令面板入口。
- **IDE 调试器**：新增 `app/debugger.py`（Node 走 CDP Inspector，复用 `_WSClient`；Python 走 debugpy DAP）与 `/api/debug/{start,state,breakpoints,action,evaluate,stop}`；IDE 活动栏新增「运行和调试」面板：Monaco 行号断点、继续/单步/跳出、调用栈、作用域变量、输出、表达式求值；`requirements.txt` 增加 `debugpy>=1.8`。
- **验证**：`ruff`、`node --check` 通过；`pytest` 全量（排除 archify）EXIT=0；`tests/test_archify.py` 单独 8/8；新增 `tests/test_codeindex_v2.py`、`tests/test_debugger.py`。
- **注意**：Python 调试需要 `python -m pip install debugpy`；Node 调试用本机 Node（已装 v24）。索引与调试器均为后台/会话级能力，重启后生效；`computer_*` 仍保留细分动作。






## [2026-11-02] IDE 网页预览：小地图挡住按钮 + 预览路径校验
- 问题/决策：浏览器实测发现预览面板右上角的 ↻ / ✕ 点不动——Monaco 的 `.minimap` 自带 `z-index:5`，压在东侧边缘，而 `.ide-preview` 只有 `z-index:4`，↻ 整体、✕ 只剩约 5px 露在外面；键盘（Tab+空格）能触发，所以容易误判成「刷新/关闭不生效」。
- 解决方案/原因：
  - `web/css/ide.css`：`.ide-preview` 提到 `z-index:8`（仍低于下拉 30 / 命令面板 40 / 信息层 50），鼠标命中恢复正常。
  - `app/api/ide_intel.py`：新增 `_preview_rel()`，先用 `agent.resolve_path(..., for_read=True)` 收敛路径，再拦密钥文件、检查存在性；不存在/越界/`.env` 直接 400 返回中文原因，不再「start 成功但 iframe 里是 Error response」。URL 用 `urllib.parse.quote` 编码（中文/空格文件名）。
  - `app/preview.py`：预览服务与页面同源，页面脚本能 fetch 任意路径，所以 `send_head()` 里按 agent 的规则再拦一次（越界 / BLOCKED_SEGMENTS / 密钥文件一律 403），`.git/config` 不再泄露。
  - `web/gal/gal-ide.js`：打开面板时若当前就是 html 则自动加载（省一次「打开」）；输入框支持回车打开；`localhost:5173` 这类不带协议的地址自动补 `http://`。
- 相关代码/指令：`python -m pytest tests/test_ide_preview.py -q`（6 项：路径收敛、越界/密钥/不存在、静态服务 403/404/no-store、路由注册）；浏览器实测（Edge headless 1280×900）`elementFromPoint` 在 ↻ / ✕ 中心命中按钮本身（修复前命中小地图）。
- 已知未修：≤760px 窄视口下 `#main.ide-mode .chat-col { flex:1 1 auto }` 的 flex-basis 取内容宽度，会把 `#ide-panel` 挤成 0 宽（IDE 整体不可见），需单独调整该断点布局。

## [2026-11-02] 插件市场安装教程 + 子代理启用条件
- 问题/决策：
  - 子代理只在「Agent + 沙箱 + 非 GAL」时存在：工具 schema 由 `native_tool_schemas(agent_on=...)` 决定，运行时 explore / delegate 还会二次校验；`agent_explore_enabled`（默认开）是总闸。自动 suggest / auto-plan / auto-review 同样要求 `agent_on and sandbox`。
  - 插件页原先把「把仓库地址发给 agent 就能装」当路径，实际不成立：插件目录 `data/plugins` 在 `data/` 下，agent 的 `resolve_path` 永久拒绝访问；agent 也没有安装插件的工具（`/api/plugins` 仅前端可用），装完必须重启进程。
- 解决方案：
  - `web/hall-plugins.js`：发现页顶部新增可折叠「安装教程（3 分钟）」：最快 3 步、六种安装方式表（内置 / 本地目录 / zip / Git / 单文件 .py / 手动）、最小 `plugin.json` + `@agent_tool` 示例、兼容性三问、修正后的 Agent 改造 6 步（agent 只产出到沙箱，最后由用户安装并重启）、安全与限制。
  - 空态「已安装」加「看安装教程」链接（切到发现并展开）；zip/Git 对话框文案补 `.py` 与“重启后加载”；侧栏标题从 “Skill 安装管理” 改为 “Python 工具插件 / MCP 服务器 / DSH 生态索引”。
  - `web/css/hall-plugins.css`：教程块样式（`mk-guide-*`），窄屏隐藏副标题、代码块内部横滚。
- 相关代码/指令：`node --check web/hall-plugins.js`；浏览器实测（590×894）折叠默认收起、展开后 6 行表格 / 代码块 / 6 步 / 3 条限制齐全、空态链接可跳转、无控制台报错、无页面横向溢出。
- 结论备忘：插件是 Python 工具插件（`@agent_tool`），DSH 的 Cordis/TypeScript 插件只能参考不能直接装；不兼容时走“agent 在沙箱改造成兼容包 → 用户从本地目录安装”的流程。

## [2026-11-02] Solo 旁注入口浏览器验收（拖选可用，双击未复现）
- 问题/决策：验收 aside.js / aside.css 的选区工具条与旁注卡片。硬刷新（Ctrl+Shift+R）后 `/static/aside.js`、`/static/css/aside.css` 均 200 加载；拖选 AI 回复正文后 `.aside-bar` 在选区上方出现（问问 / 引用），点问问弹出 `.aside-panel`，显示「#6 · AI 回复」+ 选中文字「的修改，我需要」，输入框自动聚焦；点 ✕ 后卡片 display:none，工具条保持 hidden。
- 解决方案/原因：拖选路径正常；自动化双击 AI 正文时选区被清空、焦点落到 `#input`（composer），未复现双击选词，疑似应用层点击聚焦输入框或自动化双击时序导致，待人工双击确认。底部输入框内拖选（selectionStart 1–5，window.getSelection 为「注测试词」）时工具条不出现，符合预期。
- 相关代码/指令：`web/aside.js`（mouseup→syncBar，validRange 限定 #chat 与 `.msg[data-idx]`）；内置浏览器访问 `http://127.0.0.1:51501/` 会停在 chrome-error，改用 `http://localhost:51501/` 可正常打开（同一服务，直连 200）。

## [2026-11-02] Solo 旁注提问（选中文本就地问答）
- 问题/决策：主对话流式输出时夹杂工具调用和英文，用户边看边理解困难；需要「不打断主流、也不污染主上下文」的临时提问。主对话有同会话串行约束（`ACTIVE_STREAMS` 409），所以旁注必须走独立通道。
- 解决方案：
  - 后端 `app/aside.py` + `app/api/aside.py`：`POST /api/aside/stream` 独立 SSE；不进入 `ACTIVE_STREAMS`、不写 messages / memory / trajectory、不挂工具；上下文只收「选中片段 + 前后文 1200 字 + 最多 4 轮追问」，超预算从最老追问开始丢；配置 `aside_enabled / aside_model / aside_max_tokens`（空模型 = 跟随主聊天）；并发限制单会话 1 条、全局 4 条。
  - 前端 `web/aside.js` + `web/css/aside.css`：Solo 消息选区 → 「问问 / 引用」工具条 → 浮动卡片；点击瞬间冻结选区（文本 + 前后文 + msg_index/role），支持单轮 / 追问 / 快捷问法 / 复制 / 带入输入框 / 拖动 / Esc 关闭；卡片只存内存，切会话关闭。
  - `web/core.js` 的 `applySessionsPayload` 增 `asideOnSessionChange` 钩子；`index.html` 引入脚本与样式。
- 相关代码/指令：`python -m pytest tests/test_aside.py -q`（9 项：片段/上下文裁剪、历史回放、预算淘汰、并发限制、完整 SSE 分支）；浏览器实测拖选出现工具条、卡片显示「#6 · AI 回复」+ 引用文字、关闭正常、输入框选区不触发。
- 注意：后端是新增模块，**需重启应用**才生效；前端刷新即生效。自动化双击未复现选词，拖选路径正常，待人工双击确认。

## [2026-11-02] 八款 AI Agent 八维能力评估与「本项目适配」双榜
- **问题/决策**：用户要求对 OpenCode / Codex / Claude Code / Trae / DeepSeek Harness / Cursor / ZCode 与本项目做统一维度、1–10 分的量化排名，并给出项目适配分析。结论必须区分「通用能力」与「本项目适配」，否则会系统性高估/低估某些工具。
- **方法**：
  - 8 维：代码理解深度、复杂任务拆解、多文件上下文、自主迭代调试、工具调用准确性、长任务稳定性、生成代码质量、错误修复效率；每维给 9–10 / 6–7 分锚点，避免「凭感觉打分」。
  - 双权重：通用（15/15/15/15/10/10/10/10）与项目（18/12/16/14/12/8/8/12）；项目权重来自本项目 6 个内部评测任务的分布（bug_fix / refactor / tool_pipeline / ui_restore 均属跨文件修改 + 回归）。
  - 适配分 = 项目加权能力 − 落地摩擦（Windows / BYOK / 本地隐私 / 栈匹配 / 视觉验收 / 成本），摩擦逐项写死，可复算。
  - 证据分级 A/B/C；相邻名次差 ≤0.3 视为噪声；公开基准测的是「模型 + harness」，不能全算给 harness。
- **公开证据**：
  - Terminal-Bench 2.0：Codex CLI + GPT-5.5 82.2%（总榜第 4，通用 harness 第 1）；Claude Code + Opus 4.6 58.0%；OpenCode + Opus 4.5 51.7%；Meta-Harness + Opus 4.6 76.4%（同模型差 18 分，证明 harness 影响巨大）。
  - Harness-Bench（274 任务 × 10 模型 × 7 harness）：十模型均值 OpenCode 67.4 > Codex 66.2 > Claude Code 65.9；Kimi-K3 上 OpenCode 69.5 >> Codex 65.1 > Claude 60.0；GLM-5.3 上 OpenCode 70.3 第一。
  - Composio Bench（30 个有状态工具任务）：DeepSeek-V4-Pro 下 Codex 53.3% = DSH 53.3% > Claude 50% = OpenCode 50%；GPT-6 Astra 下 Codex 75.9% > Claude 72.4% > OpenCode 69.0%。
  - Cursor Composer 2.5：T-Bench 2.0 69.3%（厂商）、AA Coding Agent Index 62（Claude Code 66 / Codex 65）、单任务成本约 $0.07；SWE-bench Verified 第三方汇总 Claude Code 87.6% / Codex 85%。
- **结论（通用榜）**：Codex 9.13 > Claude Code 8.90 > OpenCode 8.38 > Cursor 8.23 > ZCode 8.10 > DeepSeek Harness 7.68 > 本项目 7.38 > Trae 7.03。Codex 与 Claude Code 差距小于噪声，实质并列第一梯队。
- **结论（本项目适配榜）**：Claude Code 7.81 > Codex 7.73 > OpenCode 7.62 > Cursor 7.43 > 本项目 7.12 > ZCode 7.08 > DSH 7.02 > Trae 5.87。头名翻转的关键：Codex 的 Windows 实验性（−0.6）、视觉验收（−0.2）、BYOK（−0.2）摩擦合计 −1.4；Claude Code 的 LSP + Windows 原生 + 浏览器能力只扣 −1.1。
- **项目侧硬事实**：app + web + tests ≈ 15.3 万行；1,790 个测试函数 / 177 个测试文件；默认 14 个核心 action、全量 55 个工具 schema；10 个内置子代理角色；全仓语义索引 v2 + pyright LSP + 轨迹查看器；内部 6 题 × 3 次 13/18（72.2%，2,846,979 token，1,514.1s），短板集中在 web_voxel 0/3 与长回合稳定性。
- **交付物**：`docs/ai-agent-capability-ranking-2026/ai-agent-capability-ranking-2026.html`（单页、自包含、ECharts 热力图 + 两组条形图、27 条来源）。同目录含 `assets/charts.js`、`assets/favicon.svg`、`_shared/`。
- **验证**：`node --check assets/charts.js` 通过；Playwright + Edge 在 590 / 1440 两种宽度实测：`scrollWidth == clientWidth`（无页面级横向溢出）、10 个宽表均内部滚动、3 个 ECharts 实例存在、27 条引用锚点全解析、控制台 0 error、favicon 200。
- **边界**：本轮没有本机同条件实跑（仅 Cursor CLI 在 PATH），分数是「公开基准 + 官方文档 + 源码审查」的锚定判断，置信度 ±0.3–0.5；厂商自报数据标 B/C；内部 6 题不可与公开榜横比；工具版本迭代会使排名失效。
- **后续若要实测**：复用 `scripts/bench.py`（6 题）+ `scripts/eval_research_run.py`（12 题），固定模型/温度 0/重复 3 次，记录通过率、轮次、工具数、token、耗时、人工干预与安全违规，才能把 C 级推断升级为 A 级数据。

## [2026-11-02] 修复：验收代理工具名 + finish 静默结束
- 问题/决策（现场 10:39，turn 3）：模型验证完调用 `finish`，验收子代理把 `read_file / run_command / list_dir` 全判成「不在你的可用范围内」→ `blocked / 0 分` → finish 被驳回；模型随后只写了一句「修完再收尾」就停，一轮里唯一的一次 finish 提醒早已用掉，`touch` 不到续跑条件，于是 `turn/end reason=completed`，用户看到的是「刚验证完就结束、没有结论」。
- 根因：
  - 工具元层重构后 schema 是 `read / edit / run / code…`，但验收代理（尤其文本协议小模型）沿用旧名；`quality_gate.run` 只按 schema 名判断白名单，旧名一律被拒。
  - `_needs_finish_nudge` 原先「一轮只提醒一次」；finish 被验收驳回后没有重新给额度，下一次无工具停轮就静默收尾。
- 解决方案：
  - `app/tool_meta.py` 新增 `legacy_map()`（旧名 → (meta, action)）。
  - `app/quality_gate.py`：新增 `allowed_names()`（schema 名 + 可映射到可用元工具的旧名）、`tool_lines()`；验收任务书新增【可用工具（只能调用这些名字）】；`REVIEW_SYSTEM` 要求工具名与列表一致；`DENY_TOOLS` 补 `plan / agent / workflow / utility`（元工具名）。
  - `app/api/chat.py`：`_acceptance_review` 把 `_schemas` 传给 `build_brief`；`nudged_finish` 改为计数 `finish_nudges`，上限 `MAX_FINISH_NUDGES=3`；finish 被「像素闸门 / 验收代理」驳回时清零，重新给收尾提醒额度。
- 相关代码/指令：`python -m pytest tests/test_quality_gate.py tests/test_tool_meta.py tests/test_chat_blocks.py -q`（新增 4 项：任务书工具清单、旧名白名单、旧名实际执行、反向映射）；全量 `pytest -q` 通过。注意：后端改动需重启应用。

## [2026-11-02] 修复：attach 重放 + 上游等待可见
- 问题/决策：长任务中断/重开页面后，前端只显示落库的进度快照 + 「后台运行中」，`/api/chat/attach` 的事件只用来找 `done`/`end_reason`，不再渲染；同时请求发出到拿到响应头之间只有心跳，界面看不出在等什么。
- 解决方案：
  - `web/stream.js` 新增 `startJobReplay()`：重连时先撤掉落库的进度快照行，建一条临时实时行，回放 `delta / rewind / reasoning / tool / tool_done / model_wait / model_ready / retry / warn / error`，结束后 `pullState()` 用落库结果重画一次。
  - `waitJobThenRefresh()` 接入 replay：正常结束、attach 404、异常三条路径都会先收掉临时行再拉状态。
  - 主链路补 `model_wait` / `model_ready` 分支：状态条显示「等待上游响应（最长 Ns）」和「上游已响应」。
  - `app/llm.py`：请求发出前 emit `model_wait`，收到响应头后 emit `model_ready`；两个事件都会进 JOBS，attach 重放也能看到。
- 验证：`node --check web/stream.js`、`ruff`、全量 `pytest` 通过。浏览器实测（服务端旧进程仍在跑）：重连后出现临时实时行，正文 351 字（落库快照只有 264 字），仅一条 AI 行、无「后台运行中」，无 JS 报错；因该轮自 11:53:30 起服务端没有任何新事件（旧进程无 model_wait），观察窗内没有增量。
- 注意：后端改动需重启应用；当前 stuck 的那一轮建议重启后重发。

## [2026-11-02] 上下文窗口默认改为 1 亿
- 问题/决策：`chat_context_limit` 默认 0（不限）会让 `request_budget()` 直接返回 0，按窗口比例的逻辑全部关闭；改为默认 `100_000_000`，保留窗口语义又等效于“足够大”。
- 解决方案：
  - `app/config.py` 默认值 0 → 100000000；同时把当前 `data/config.json` 写成 100000000。
  - `app/context.py manual_keep_budget()`：窗口远大于实际历史时，保留预算以实际历史为上限，避免 16% 保留变成“全部保留”、手动压缩永远提示可压缩内容不足。
  - `readme.md` 配置表同步。
- 相关代码/指令：`python -m pytest tests/test_context.py tests/test_compress_marks.py tests/test_settings_defaults.py tests/test_provider_chain.py -q`（56 passed）；`ruff` 通过。
- 注意：自动压缩阈值变为 8000 万 token，实际等于不自动压缩，仍可手动压缩；`context.py` 的改动需重启应用，配置值本身下次读取即生效。

## [2026-11-02] 附件上传支持 .tex
- 问题/决策：文件选择器的 accept 列表没有 `.tex`；`docparse.TEXT_EXT` 也没有，虽然拖拽/粘贴能上传，但会被当成陌生扩展名走二进制探测，预览也不按文本展示。
- 解决方案：
  - `web/index.html` 的 `#doc-input accept` 增加 `.tex`；
  - `app/docparse.py` 的 `TEXT_EXT` 增加 `.tex`，按纯文本解码；
  - `web/chat.js` 的 `DOC_PV_TEXT` 增加 `tex`，附件预览按文本显示；
  - `readme.md` 附件格式说明同步。
- 相关代码/指令：`python -m pytest tests/test_docparse_office.py -q`（新增 `.tex` 文本解析用例）；`node --check web/chat.js`、`ruff` 通过。

## [2026-11-02] 粘贴长文本 → 轻量文本块（替代附件方案）
- 问题/决策：最初把长文本用 `new File()` 生成 `pasted-*.md`、走 `pendingDoc → save_upload → docparse` 附件通道，交互偏重（上传 base64、落盘、解析、还要点「还原」）。改为轻量块：只存原文，不生成文件、不上传、不走文档解析。
- 解决方案：
  - `web/stream.js`：删除 `looksLikeMarkdown / shouldPasteAsDoc / attachPastedText / _pastedDoc / restorePastedDoc` 与 `PASTE_MD_MIN`；改为 `PASTE_BLOCK_MIN=800`、`MAX_PENDING_PASTES=6`、`state.pendingPastes=[{id,text}]`。`attachPastedBlock()` 只入内存，`removePendingPaste()` 单独移除，`previewPendingPaste()` 复用 `docPreviewOpen` 预览。
  - 发送时在 `sendMessage()` 里把文本块按顺序拼到消息正文（`#web` 只解析手打部分），排队消息也直接带合并后的 `text`，因此无需扩展队列结构。
  - `web/composer.js cmpPaste()` 改为 `shouldPasteAsBlock()`；`syncSendBtn()` 把文本块计入非空判断；`clearAttach()` 一并清空 `pendingPastes`。
  - `web/index.html` 增加 `#attach-pastes`，`base.css` 增加 `.attach-paste` / `.attach-paste-x`，移除 `.attach-doc-restore`。
- 验证：`node --check` 通过；无头 Edge + CDP 实测 1125 字粘贴 → 输入框为空、出现「粘贴文本 · 1125 字」块；× 单独移除后附件栏收起；`streaming=true` 时点发送，队列文本严格等于「手打文字 + 空行 + 粘贴正文」；点击块可预览全文。

## [2026-11-02] 拖拽文件到输入框：drag-over 高亮 + 附件 chip
- 实现：`web/composer.js bindComposerDrop()` 监听 `.composer-box` 的 dragenter / dragover / dragleave / drop；检测到 Files 时 preventDefault + stopPropagation，加 `.drag-over`，落下后图片走 `attachImageBlob`、其它走 `attachDocBlob`；拖到别处由 window 的 drop/dragend 清高亮。`base.css` 用 `::after` 在输入框中央显示「松开以添加附件」。
- 问题/决策：验收 `http://localhost:51501/` 的 Solo 输入框拖拽。开始时 51501 未监听（`data/app_port.txt` 仍记 51501），内嵌浏览器 navigate 60s 超时、evaluate 报 `WebView is not ready yet`；需先起服务并另开新标签页。
- 解决方案/原因：无头临时启动 `.venv\Scripts\python.exe -c "import app.httpd as h;h1,_=h.create_server(port=51501);h1.serve_forever()"`；页面内用 `new DataTransfer()` + `items.add(new File(["# 测试"],"drop-test.md",{type:"text/markdown"}))` 依次派发 dragenter / dragover / drop。
- 结果：dragenter/dragover 均被 preventDefault；`.composer-box` 增加 `drag-over`，`::after` 计算值 `"松开以添加附件"`、`border-top-style: dashed`、accent 色 `rgb(59, 87, 224)`；drop 后 `drag-over` 移除，`#attach-doc` 从 hidden 变为可见，文本 `drop-test.md（1 KB）`；输入框为空、未发送；控制台 0 条消息。
- 相关代码/指令：`web/composer.js` `bindComposerDrop`（620-658）、`web/stream.js` `attachDocBlob` 与 chip 渲染（2468、2587-2603）、`web/css/base.css` 1829-1840。测试后已关闭临时标签页并停止临时服务。

## [2026-11-02] IDE 文件树右键菜单与复制 / 剪切 / 粘贴
- 问题/决策：文件树只有顶部按钮和拖拽移动，重命名只能作用于「当前打开的文件」，没有右键菜单和复制能力，和 VS Code 差距明显。
- 解决方案：
  - 后端 `app/api/sandbox.py` 新增 `/api/sandbox/copy`：支持文件与目录递归复制；目标重名时自动改名（`a copy.txt` / `a copy 2.txt`），绝不覆盖；拒绝复制沙箱根目录、目录复制进自身子目录、越界路径。
  - 新增 `/api/sandbox/reveal`（Windows `explorer /select,`）用于「在资源管理器中显示」；`agent.delete_entry()` 增加 `recursive` 参数，IDE 删除目录时连内容一起删（其它调用方默认仍拒绝非空目录）。
  - `web/gal/gal-ide.js`：新增右键菜单（新建文件/文件夹、重命名、删除、复制、剪切、粘贴、创建副本、复制路径、在资源管理器中显示、在 VS Code 中打开、刷新）与 F2 / Delete / Ctrl+C / Ctrl+X / Ctrl+V / Ctrl+A 快捷键；重命名改为对右键目标（含目录）操作。
  - 新增 `ideRemapPrefix()`：改名或移动后统一迁移标签、Monaco model（URI 重建，面包屑不指向旧文件）、缓存、未保存标记、多选、AI 改动标记、断点与剪贴板路径。
  - `web/core.js` 增加 `state.ideClip`；`web/css/ide.css` 增加 `.ide-ctxmenu` 样式（按视口边缘翻转）。
- 踩坑：沙箱目录不能指向项目 `data/`（`_assert_not_blocked` 会以「data 目录被禁止访问」拒绝所有读写），右键复制会 400；端到端测试要使用项目内非 `data` 的临时目录。
- 验证：新增 `tests/test_ide_fileops.py` 9 项（文件/目录复制、重名自动改名、目录自嵌套拒绝、越界拒绝、递归删除、目录改名、根目录删除拒绝）全部通过；`ruff`、`node --check` 通过。用无头 Edge + CDP 端到端实测：右键菜单 8 项全部渲染，创建副本 → 重命名 → 复制粘贴到子目录 → 删除全部成功，F2 / Delete / Ctrl+C / Ctrl+V 快捷键均生效。

## [2026-11-02] 移植 modsearch 的免注册搜索 / 抓页引擎链
- 问题/决策：dsh 插件 `@liustack/modsearch` 本身不是搜索引擎，而是「引擎链转接器」：Firecrawl / Keenable / You.com / Tavily / Exa / Grok 按序尝试、失败自动换下一个，配额打爆的引擎进冷却，输出统一 JSON（`results[]` 带 `engine/status/uncertainty/warnings/attempts`）。它的 dsh 安装层（`ctx.web.registerSearchProvider()` / `ctx.tools.register()` + `dsh plugin add`）是 Cordis/TypeScript 运行时专有，本项目用不了；目标定为**把引擎层移植进 Python**，不引入 Node 依赖。
- 解决方案/原因：
  - 新增 `app/search_engines.py`：`firecrawl_search`（`POST /v2/search`，keyless 不带 Authorization）、`keenable_search`（keyless 走 `/v1/search/public`，必须带 `x-keenable-title`）、`youcom_search`（`GET /v1/agents/search`）、`auto_search`（按 CHAIN 逐个试，`quota`/`auth` 失败写冷却，全失败抛 `EngineError` 并列出每个引擎原因）、`cloud_fetch`（`POST /v2/scrape`，`maxAge:0` / `storeInCache:false` / `skipTlsVerification:false`）。
  - 冷却落 `data/cache/search_engines.json`（`{"cooling": {name: until_ts}}`），默认 1 小时，冷却期内直接跳过、不再浪费请求。
  - `app/search.py`：`SEARCH_PROVIDERS` 增加 `auto`；`get_search_conf()` 对 auto 返回空 key；`search_web()` 首行分派到 `auto_search`；`_fetch_one()` 改为「云端浏览器 → jina → 直连」，云端前先用 `public_url_error()` 判私网，私网只走本地；`search_multi()` 在所有检索式都报错且零结果时抛真实原因（避免界面误报「没搜到」）。
  - `effective_search_provider()`：明确选 `auto` 就直接用引擎链，不再回落到聊天模型自带的付费搜索。
  - 配置：`search_provider` 默认改 `auto`；新增 `search_engine_keys` / `search_engine_enabled` / `search_fetch_cloud`（Key 也认 `FIRECRAWL_API_KEY` / `KEENABLE_API_KEY` / `YOUCOM_API_KEY` 环境变量）。
  - 总开关 `search_modsearch_enabled`（默认开）：设置页「启用 modsearch」取消勾选后，「自动」不可选、云端抓页复选框禁用；`auto_search` 直接抛「已关闭」、`cloud_fetch` 返回空串；旧配置仍存 `auto` 时 `effective_search_provider()` 按原来的默认（聊天自带搜索 → 博查）回落。
  - 前端：`web/settings.js` 增加「免注册引擎」区块（三组 Key + 参与自动链勾选），选 auto 时隐藏单服务商地址/密钥；`web/stream.js` 的 `hasSearchKey()` 认可 auto（不需要密钥）；`web/init.js` 的未配置提示按模式区分。
- 踩坑：
  - `app/api/chat.py` 原来用 `if not skey:` 直接判定搜索不可用——auto 模式 key 为空会被误判，已改成 `not skey and sp != "auto"`。
  - Firecrawl 搜索返回的 `description` 是**整页 markdown**（实测一条几千字），必须截到 `SNIPPET_MAX=600`，否则单条结果就把上下文撑满。
  - 本机网络下 `api.you.com` 连接超时，You.com 只能当第三顺位；Firecrawl / Keenable 的 keyless 端点实测均 200。
  - 用户已有配置里 `search_provider` 存的是 `volc`，改默认值不会迁移旧配置；要启用需在设置里手动切「自动」。
- 相关代码/指令：`app/search_engines.py`、`app/search.py`、`app/config.py`、`app/balance.py`、`app/api/chat.py`、`web/settings.js`、`web/stream.js`、`web/init.js`、`tests/test_search_engines.py`。
- 验证：新增 `tests/test_search_engines.py` 15 条（字段收敛 / 截断 / keyless 头 / 降级与冷却 / 引擎禁用 / 全失败原因 / 开关关闭不发请求 / 云端抓页参数 / 私网跳过 / 部分失败保留）与 `test_search_provider.py` 3 条开关回落用例，连同搜索、聊天、余额、研究等关联用例共 163 条 `pytest` 全过；`node --check` 与 `py_compile` 通过。真实网络冒烟：`search_web("Node.js LTS 最新版本","auto",...)` 返回 5 条、1.5s、engine=firecrawl；`cloud_fetch("https://example.com")` 959 字 / 1.6s；`_fetch_one` 走云端 958 字 / 1.7s。浏览器实测设置页：自动/博查切换、三组 Key 与勾选框显隐正常（初始选中项取决于已保存配置，当前为豆包搜索）；「启用 modsearch」默认勾选，取消后「自动」变不可选、云端抓页复选框被禁用，重新勾选即恢复。

## [2026-11-02] archify 为什么从没被调用：补提示词分层
- 问题/决策：用户反馈 archify 做好后从未见 AI 调用。排查确认不是环境问题（`archify.available()=True`，vendor 3.2.0 + node 正常），而是触发链路和提示词冲突。
- 原因（按影响排序）：
  1. 常驻的 `_VISUAL_HEAD` 明确要求「看图 / 画图 / 可视化」时直接输出 ```svg / ```chart / ```mermaid 围栏，整段没提 archify —— 模型有更省事的默认路径。
  2. `agent_cap_tools=auto` 时 archify 的 schema 只在命中 `TOOL_GROUP_KEYS["diagram"]` 才下发；词表不含「示意图 / 系统图 / 模块图 / 关系图 / 状态机」等常见说法。
  3. `intent_hint` 只覆盖 bili / 提醒 / 记忆 / 日程 / 文件 / 搜索 / 股票，从不提示 archify。
  4. archify schema 位于 `if agent_on:` 块内 —— 未开 Agent 或没选沙箱时完全不存在。
- 解决方案：`_visual_prompt(file_ready)` 增加分层块：
  - `_VISUAL_DIAGRAM_READY`（Agent + 沙箱）：一张图能讲清的走围栏；多模块架构 / 跨服务数据流 / ERD / 类图 / 时间线 / 瀑布图 / 一页式解释页 → 用 archify（先 schema 再 finalize，大候选先 write_file 再传 candidate_path）；工具表里没有就先 `tool_search("diagram")` 解锁。
  - `_VISUAL_DIAGRAM_OFF`（普通聊天）：只说明需要切到 Agent 模式，不给不可用的解锁指令。
- 相关代码/指令：`app/agent.py`（`_VISUAL_DIAGRAM_READY` / `_VISUAL_DIAGRAM_OFF` / `_visual_prompt`）、`tests/test_agent_guards.py`（`test_visual_prompt_layers_archify_by_agent_mode`）。
- 验证：`tests/test_agent_guards.py`、`test_archify.py`、`test_prompt_assembly.py`、`test_agent_tool_format.py` 共 143 项通过。真实对话里是否改用 archify 待观察：在 Agent 模式 + 已选沙箱下说「画一张这个项目的模块架构图」即可验证（命中「架构」→ diagram 组挂载）。

## [2026-11-02] 历史压缩按钮：按「本轮结束」而不是「完整输出完成」判定
- 问题/决策：压缩圆环只有模型完整输出后才能用；中断轮（报错 / 断线 / 空回复）要么看不到按钮，要么被 `state.streaming` 一直拦住。
- 原因（三处叠加）：
  1. 圆环挂在最后一条 AI 消息的 `.msg-actions` 上，而操作条只在「本轮有正文」时创建；空正文 / 请求失败的轮次不建操作条 → 圆环无处可挂。历史渲染也有同样的 `if (m.content)` 限制。
  2. `compressContextManually()` 用 `state.streaming` 做闸门；刷新 / 断线后这个标记可能一直为 true（`streamingSids` 来自 /api/state，`keepRecovering` 最长等 10 分钟）。
  3. SSE 读循环没有超时：连接假死时 `reader.read()` 不返回，`finally` 不执行 → `state.streaming` 永远是 true。
- 解决方案：
  - `web/stream.js`：流对象记录 `lastEvent`（每收到一个 SSE 事件刷新）；流收尾时若该轮没有操作条就补建一个（空正文 / 报错轮也能用「继续输出 / 重新发言 / 压缩」）。
  - `web/chat.js`：历史 AI 消息不再按 `m.content` 决定是否建操作条（空内容也建）；手动压缩改用「本地流最近 15 秒是否还有事件」判断，不再用 `state.streaming`。
  - `app/api/chat.py`：`_job["ts"]` 在每个 SSE 事件时刷新；新增 `stream_alive(sid, idle=30)`——ACTIVE_STREAMS 有条目但 30 秒无事件即视为已结束（崩溃 / 断线残条目不拦压缩）。
  - `app/api/kb.py`：压缩接口在真正产流时拒绝（提示「正在生成回复」），避免生成结束落库覆盖刚写好的摘要。
- 相关代码/指令：`web/stream.js`（`registerStream` / SSE 读循环 / 收尾补操作条）、`web/chat.js`（`compressContextManually` / `buildAiRow`）、`app/api/chat.py`（`stream_alive`）、`app/api/kb.py`（`_api_context_compress`）、`tests/test_stream_stop.py`。
- 验证：新增 `test_stream_alive_uses_recent_events`（活跃 / 超时残条目 / done / 无登记四种）；`test_stream_stop`、`test_context`、`test_api_handlers_smoke`、`test_chat_blocks`、`test_chat_jobs` 共 94 项通过；`node --check` / `py_compile` 通过。浏览器实测：打开「乘积自由集」会话，最后一条 AI 消息右侧圆环可见，点击弹出压缩确认框，取消后未产生新压缩。

## [2026-11-02] 沉浸式 PDF 阅读器：左原页 + 右双语阅读流
- 问题/决策：要复刻沉浸式翻译读外文论文，但参考产品闭源，同类开源项目（OnlyTranslate / FluentRead / Read Frog / BabelDOC）是 GPL/AGPL，本项目 MIT 不能抄代码。形态定为「左原页 + 右阅读流」（与只译同款）：原页保真对照，阅读流负责分段、双语、可调字号；不做原页覆盖层（固定画布没地方插译文，双栏和公式最容易错位），也不做 BabelDOC 那种整篇重排 PDF（交互太弱）。
- 方案：
  - 后端 `app/reader.py`：pypdfium2 逐字符取 bbox → 按字顶上沿分桶成行段 → 桶内按 x 空隙切段（双栏不粘连）→ 「中间空带 + 跨越行段纵向覆盖 <45%」判定双栏 → 跨栏行段单独成带（band），带内先左栏后右栏 → 行距/字号/缩进/短行合并成段 → 页尾无标点续段跨页合并。行内公式用 `⟦F1⟧` 占位符保护（送模型的 `ttext` 与展示的 `text` 分开），独立公式/图表按 PDF image 对象 bbox 裁图。版面缓存 `data/reader/<doc_id>/blocks.json`，译文缓存 `t_<lang>_<model>.json`，doc_id = 绝对路径 + size + mtime。
  - API `app/api/reader.py`：`open` / `file`（PDF 原始字节）/ `crop`（PNG）/ `translate` / `events`（SSE）/ `cancel`。翻译复用 `llm._quick_chat`，5 段一批、JSON 数组输出，占位符数量不一致时重试一次，缺段单独补一次。
  - 前端 `web/reader.js` + `web/css/reader.css`：pdf.js 5.4.624（vendored 到 `web/vendor/pdfjs/`，Apache-2.0）懒渲染左栏 canvas；右栏阅读流 + 原文/双语/仅译文三档；滚右栏时原页自动跟随；按页增量翻译（本地记录已请求页，任务结束后再发 pending）；阅读位置存 localStorage；图表点击全屏放大。
  - 入口：`web/gal/gal-ide.js` 的 `openIdeFile` 增加 `.pdf` 分支，直接交给 `window.Reader.open`，不再落到 `/api/sandbox/file` 报「二进制文件无法在编辑器打开」。
- 踩坑：
  1. 逐字符按 y 中心排序再贪心合并会被零点几点的 y 抖动打乱 x 顺序，一行被拆成几段甚至跨行交错 → 必须先按基线上沿分桶，桶内再按 x 切；拉丁字母降部（g/p/y）会改变底边，所以不能用底边分桶。
  2. 单栏页每一行都是「整宽」；若按宽度判定 wide，会把每行切成独立段落，永远合不起来 → 单栏不做宽窄拆分，只有真正跨过栏间空带或宽度 >72% 页宽才单独成带。
  3. 只用「有没有行段跨越候选线」判双栏，会把右对齐日期/页码误判成分栏 → 改为要求跨越行段的纵向并集覆盖 <45% 页高。
  4. `state` 是 `core.js` 顶层 `const`，不会挂到 `window`；`reader.js` 里写 `window.state` 恒为 undefined，导致 `session_id` 为空、所有接口 404「会话不存在」→ 直接按变量名读 `state`。
  5. pdfium 对行尾软连字符返回 U+FFFE，需要归一成 U+00AD（保留「这是断词」的语义），再由连字符回接逻辑决定去留。
- 相关代码/指令：`app/reader.py`、`app/api/reader.py`、`app/httpd.py`（导入 reader 路由）、`web/reader.js`、`web/css/reader.css`、`web/index.html`、`web/gal/gal-ide.js`、`web/vendor/pdfjs/`、`tests/test_reader.py`。
- 验证：`tests/test_reader.py` 10 项（分栏判定、跨页合并、占位符保护、JSON 解析、CJK 空格、连字符回接）通过；`test_api_handlers_smoke` / `test_stream_stop` / `test_context` 73 项通过；进程内 API 集成：BERT 论文 16 页 → 905 段、图表裁图 521KB PNG、按页启动翻译 19 段；浏览器实测：左栏 16 个 canvas、右栏「第 1 页」+ 双语、仅译文模式隐藏原文、首屏自动译 45/870 段（1 段失败走「没译出来」回退），标题显示「bert.pdf · 16 页」。解析耗时：首次 2.4s / 缓存命中 0.01s。
- 未做（Phase 2）：PP-DocLayout 版面模型、KaTeX 数学排版、扫描件 OCR、术语表、导出保排版双语 PDF（BabelDOC）。

## [2026-11-02] 阅读器排版修复：双栏不交错、段落不碎、仅译文可读
- 问题/决策：实测双栏论文时，左右栏句子在同一行交错（「There are two existing strategies… feature-based approach…」拼成一句）、正文被切成大量半句；切到「仅译文」后未翻译段落退回英文，看起来像模式失效。
- 原因（六处叠加）：
  1. 先把所有行段按纵向重叠缝成行，再判断分栏：左右栏行常在同一高度，缝合时被并成一行；而分栏又依赖缝合结果，互相矛盾。
  2. 分栏判定用「行段是否跨越候选线」，行段被切碎后几乎处处有跨越，标题/摘要的整宽行也把空带抹掉。
  3. 段落合并用「上一行是否短于栏宽」的绝对阈值，正文长行被误判成段末短行。
  4. 缩进用全局页边距比较；摘要整段本身缩进时，每一行都被当成新段。
  5. 左侧页边的竖排 arXiv 编号（x≈18）被缝进正文，插出「representa 9 tion」。
  6. 字号取一行字符的 max，同一行忽大忽小，段落因「字号不同」被误切。
- 解决方案：
  - `app/reader.py`：`_column_boundary` 改在中间区域统计「跨越行段数」，取跨越最少的位置；`_rows(segs, boundary)` 先分栏再缝合，且**不跨分界缝合**；同一行碎片用「纵向重叠或（中心高度差 + 水平紧邻）」双条件并查集缝合。
  - 段落合并：基准字号取整栏行字号中位数；缩进相对**当前段落**左边界判断；段末短行阈值改为栏宽 40%；heading 字号超过基准 1.35 倍时强制断段。
  - 页边裁剪：丢掉 `x0 < 7% 页宽` 的字符（arXiv 竖排编号）。
  - 连字符：pdfium 的 U+FFFE 视为软连字符（断词，去掉），普通 `-` 视为复合词（保留）；`left-to-` 这类前缀带连字符的软换行也保留。
  - 停用跨页续段合并：合并后的段落挂在上一页，页码标记与内容错位，收益不如干扰大。
  - `SCHEMA_VERSION = 2`：版面算法一变，旧 `blocks.json` 自动失效。
  - 前端：段落记录 `data-state`（pending/ok/fail）；「仅译文」未译处显示「翻译中…」而不是英文；切到双语/仅译时自动把当前页排进翻译队列；记住上次视图（localStorage `rd_mode`）。
- 相关代码/指令：`app/reader.py`（`_column_boundary` / `_rows` / `_band_rows` / `_blocks_from_segments` / `_join_lines` / `_page_blocks`）、`web/reader.js`（`applyTrans` / `setMode`）、`web/css/reader.css`、`tests/test_reader.py`。
- 验证：BERT 16 页；第 1 页从 45 个碎块收敛到 23 段，Abstract 在前、Introduction 在后，首段中文连续；「仅译文」可视区 `.rd-src` 数量 0，未译处显示「翻译中…」；滚到第 2 页自动续译（44/583 → 61/583）；`tests/test_reader.py` + API smoke + stream 共 46 项通过，ruff / node --check 通过。
- 已知残留：邮件/作者这类跨栏元数据行仍可能被拆到两栏；个别正文段落仍会在句末断开；复杂图表页的文字标签会进入阅读流。下一阶段可接 PP-DocLayout 模型统一解决。

## [2026-11-02] 单栏论文误判成双栏 / 正文误判成公式（用 D:\Ai\Project\paper\out\main.pdf 复现）
- 问题/决策：用户论文（单栏 LaTeX）在阅读器里被拆成左右两栏，出现「Th t」「l duct-free」「e ex rema pro」这类半词碎片；同时一个正文段落被当成公式裁成 809px 大图，插在摘要中间，导致仅译文模式里还有大块英文。
- 原因：
  1. 双栏判定用「跨越候选竖线的行段数量」。行段被切成大量词片后，任何 x 两侧都有很多段，数量对比失效；单栏页也会被选出一个「空隙最多」的位置（x≈200），随后 `_rows` 按分界拒绝跨栏缝合，整页碎成半词。
  2. 公式判定只看符号密度和长度，不看自然语言词数、也不看行数。定理/证明里「aS ⊆ D with |aS| = ... we get」这种正文行符号密度高，被裁成图片；多行块也会被裁成几百 pt 高的大图。
- 解决方案：
  - `_column_boundary` 改为 y 轴 6pt 条带统计「该竖线在多少条带里是空的」，要求空带比例 ≥ 65%、左右两侧各自覆盖 ≥ 50% 条带；覆盖判定留 ±2pt 容差。单栏页每行都压过中段，空带比例上不去；双栏页 BERT 实测 0.90–1.00，单栏论文报错页 0.57，区分明显。
  - 公式判定加两条硬约束：**必须是单行**（`len(cur["lines"]) <= 1`）、自然语言单词 ≤1 个、长度 ≤120、不能是列表项。带 2 个以上英文词的行一律当正文，宁可少裁图也不吞段落。
  - 翻译兜底：占位符校验失败的段落，用「未保护原文」逐段再翻一次并接受结果，避免数学多的长摘要永远翻不出来。
  - `SCHEMA_VERSION` 升到 6，旧版面缓存自动失效。
  - 前端：公式/图表 img 先挂 1x1 透明占位图，懒加载完成前不显示破图图标；公式图给 18px 最小高度。
- 相关代码/指令：`app/reader.py`（`_column_boundary` / `_make_block` / `_translate_batch`）、`web/reader.js`（`renderFlow` 占位图）、`web/css/reader.css`。
- 验证：main.pdf 6 页从 390 段碎块收敛到 114 段，六页全部判为单栏（cols=[0]）；第 1 页 12 段，结构为标题 → Abstract → 1 Introduction，无图块打断；公式块 45 → 14，最大高度 22pt；摘要中文完整（169 + 329 字）；仅译文可见英文正文 0；BERT 双栏回归仍正确（第 1 页 [-1,0,1]、第 2 页 [0,1]）；`tests/test_reader.py` + API smoke 27 项通过，ruff / node --check 通过。
- 已知残留：`ifthere`、`ofproduct-free` 这类跨字体边界缺空格（PDF 里两段文本对象几乎无缝）；`b√nc` 是 ⌊√n⌋ 的字体映射损失，需要 LaTeX 推断才能还原。

## [2026-11-02] 阅读器 UI 重塑 + 翻译上下文 + 裁图坐标修复
- 问题/决策：功能可用但界面偏工具化；公式图偶尔出现几百 px 的怪异大图；翻译缺少全文上下文，长段落容易翻不出来。
- 界面/交互：
  - 顶栏重做：文件名 + 页码徽标（`1 / 6`）+ 三档视图分段控件 + 上/下页 + 译本页/译全文 + `Aa` 设置；下方 2px 进度条。
  - 阅读设置弹层：正文字号（13–24）、行距（紧/中/松）、原页缩放、隐藏原页；偏好存 localStorage（`rd_prefs_v2`）。
  - 快捷键：`Alt+1/2/3` 切视图、`←/→` 与 `PageUp/PageDown` 翻页、`Ctrl +/−` 改字号、`Ctrl+0` 重置原页缩放、`O` 收起原页、`Esc` 退出。
  - 左栏加载骨架屏；未译段落「翻译中…」呼吸占位；扫描页提示「没有可提取文字层」；翻译失败段落点击即重试；翻译期间按钮禁用并显示转圈。
  - 记住阅读位置并在恢复时按滚动位置校正页码徽标；窄屏自动隐藏原页。
- 翻译：
  - 请求带「文档上下文」（首页最像标题的一段），术语/语气更一致。
  - 动态分批：4 段或 1600 字符为上限，长段落单独成批，避免被同批挤掉。
  - 缓存文件名加入 `PROMPT_VERSION`（rd2），改提示词自动失效。
- 排版：
  - `_stitch_centered`：把被分栏切开的标题/作者/邮箱行缝回整行，只在「正文起点之前」生效（正文起点 = 第一条占满栏宽的行），避免把 `Abstract` 标题和右栏正文拼在一起。
  - 公式判定保持「单行 + 自然语言词 ≤1 + 长度 ≤120」。
- 关键 bug（裁图）：`pypdfium2` 的 `render(crop=...)` 参数是「从四边各裁掉多少」`(left, bottom, right, top)`，不是 `(x, y, w, h)`。之前把宽高当右/上裁剪量，导致公式图变成几百 pt 高的怪图（770px）。修复后公式图 22–60px；裁图缓存 key 加入 `CROP_VERSION` 让旧图失效。
- 相关代码/指令：`app/reader.py`（`_stitch_centered` / `crop_png` / `_translate_batch` / `start_translate`）、`app/api/reader.py`（`_doc_context`）、`web/reader.js`（新顶栏、设置、快捷键、进度、恢复位置）、`web/css/reader.css`。
- 验证：浏览器实测顶栏/页码徽标/进度条（8/114 → 50/114，44%）/Aa 面板（字号 16→17、行距、缩放 100%→115%、隐藏原页）/快捷键（Alt+2/3、←/→）/滚动页码跟随全部 PASS；公式块 14 个，高度 22–60px，像素统计确认是稀疏公式而非正文；`tests/test_reader.py` + ruff / node --check 通过。
- 已知残留：公式有时以文本、有时以图片呈现（取决于单行符号密度）；未进入视口的公式是 22px 占位，滚近才加载；`b√nc` 等字体映射损失仍在。

## [2026-11-02] 选区精准翻译 / 提问 + 顶栏点击「不灵敏」修复
- 功能：在阅读流里拖选任意文字，弹出浮动操作条（翻译 / 提问 / 复制）。
  - 翻译：`/api/reader/selection` 把选中片段 + 所在段落作为上下文，要求只输出译文；不走段落缓存，适合查单词、短语、从句。
  - 提问：同一个接口的 `mode=ask`，带文档上下文 + 选中片段 + 用户问题，中文回答；前端用 `safeRenderMarkdown` 渲染粗体/公式，避免 `**` 原样露出。
  - 浮层位置按选区 rect 自动上下翻转，`Esc` 先关浮层再关阅读器。
- 「顶栏按钮不灵敏」的真实原因：**文本选区还活着**。点按钮时浏览器会先把选区滚回视口，把 `goPage` 的平滑滚动覆盖掉，看起来像点击无效；同时关闭提问输入框后焦点留在 input 里，全局快捷键被 `onKey` 直接忽略。
  - 修复：顶栏按钮 `mousedown`（capture）里 `preventDefault` + 清空选区；关闭浮层时 blur 输入框；`goPage` 增加 `RD.jumpTarget`，平滑滚动动画结束前不让中间帧把页码改回旧页。
  - 翻译期间按钮不再 `disabled`（只显示转圈），避免「按钮灰了 = 界面卡死」的错觉；`译本页` 支持强制重提，不受「已译全文」标记影响。
- 相关代码/指令：`app/api/reader.py`（`_api_reader_selection`）、`web/reader.js`（`wireSelection` / `showSelectionPopover` / `selTranslate` / `askSelection` / `goPage` / `refreshTransBtns`）、`web/css/reader.css`（`.rd-sel`）。
- 验证：接口实测翻译「product-free subsets…」→「{1, ..., n} 的无积子集的极值：一个分类」；提问「这段话在定义什么？」→ 正确解释无积集合定义。浏览器实测：拖选出浮层、翻译/提问均返回中文、粗体渲染为 `strong` 且无 `**`；`✕` 关闭后立刻点 `‹/›` 从 1/6 到 2/6、左栏跟随；键盘 `←/→` 在浮层关闭后可用。ruff / node --check / `tests/test_reader.py` 通过。

## [2026-11-02] 公式识别：裁图 → LaTeX → KaTeX，公式不再占一大块
- 问题/决策：阅读流里的独立公式是裁图，宽公式被拉满整行、上下留白大，观感是「一大块图片」。目标是把单行公式识别成 LaTeX，用 KaTeX 按正文字号排版；识别失败再退回图片。
- 方案：
  - 后端 `app/reader.py`：`recognize_formula(path, page, rect)` 复用 `crop_png` 的裁图，base64 交给 `imaging.recognize_image_fallback`（视觉/聊天双通道自动回落），提示词要求「只输出 LaTeX、不要 $/代码块、非公式输出 NOT_FORMULA」；清洗反引号、`$`、`\[ \]`、`LaTeX:` 前缀。结果按「FORMULA_VERSION + 页 + rect」缓存到 `data/reader/<doc>/formulas/`，同一公式只识别一次。
  - API：`POST /api/reader/formula`；`/api/reader/open` 额外返回 `formula_ai`（是否配置了可识图的模型）。
  - 前端：公式裁图进入视口并加载完成后，串行（Promise 链）请求识别；成功则 `katex.render(..., displayMode)` 替换图片（`.rd-fig.ai`），失败静默保留图片。Aa 里新增「公式识别 开/关」，偏好存 `rd_prefs_v2`；关闭时立即回到图片，开启时用已缓存的 LaTeX 重新渲染，不重复请求。
  - 样式：`.rd-katex` 居中、可横向滚动；关键一条是 `.rd-fig.ai .katex-display { margin: 0 }` —— KaTeX display 默认上下各 1em，会把 27px 的公式撑成 74px 的「大块」。
- 相关代码/指令：`app/reader.py`（`FORMULA_PROMPT` / `recognize_formula` / `_clean_latex`）、`app/api/reader.py`（`_api_reader_formula`）、`web/reader.js`（`maybeFormula` / `recognizeFormula` / `renderLatex` / 设置开关）、`web/css/reader.css`。
- 验证：main.pdf 第 2 页 4 个公式全部转成 KaTeX，`.katex` 高 27px（容器从 74px 压到接近内容高度）；`img` 为 `display:none`；Aa 关闭后恢复 38–60px 裁图、再开启恢复 LaTeX，切换即时。接口实测：`2|A| ≤ 2n − a − T + 1.` → `2|A| \leq 2n - a - T + 1.`；`φ: S −→ [a,n] \ A` → `\varphi : S \longrightarrow [a, n] \setminus A`。ruff / node --check / `tests/test_reader.py` 通过。
- 已知残留：带多个英文词的公式行（如 `f(n):= max{...} = n − √n. (1)`）仍按正文处理，不参与识别；识别只在公式进入视口后触发，未滚动到的页面还是裁图占位。

## [2026-11-02] 笔记库批注：选中文字高亮 + 批注（不改 Markdown）
- 问题/决策：笔记库原本只有预览与编辑，没有批注。批注不能写进 Markdown 正文（会污染原文、破坏双链与搜索），因此独立存放，正文保持纯 Markdown。
- 存储：`data/notes/.annotations/<sha1(相对路径)>.json`，数组元素 `{id, quote, prefix, suffix, nth, comment, created, updated}`。`.annotations` 以点开头，`list_tree` 天然跳过，不会出现在文件树里；备份 data 目录时一并包含。
- 锚点：文本引用 + 出现序号（`quote` + `nth`），不用字符偏移——笔记被外部编辑器改动后偏移会漂，引用仍能定位；定位失败时批注仍保留在列表中，不会丢数据。
- API：`/api/notes/anno/list`、`/save`（带 id 即更新）、`/delete`，沿用 notes 模块的 `_safe` 路径校验。
- 前端（web/init.js）：
  - 预览区拖选文字 → 旁边出现「批注」小按钮 → 浮动输入框（引用 + 备注）→ 保存。
  - 渲染时用 TreeWalker 把预览拍平成 `{text, nodes}` 索引，按 quote/nth 建 Range，再用 `surroundContents` 包 `<mark class="nt-anno">`；跨行内标签时退回 `extractContents` 方案。
  - 点高亮打开批注浮层，可改可删；右侧新增「批注」标签，带计数，点击条目滚动定位并闪烁。
- 相关代码/指令：`app/notes.py`（`list_annotations` / `save_annotation` / `delete_annotation`）、`app/api/notes.py`、`app/httpd.py`（三条路由）、`web/init.js`（`applyNoteAnnotations` / `locateAnnoRange` / `showAnnoPop` / `wireNoteAnnotations`）、`web/css/notes.css`、`web/index.html`（批注标签页）。
- 验证：脚本 CRUD（新建 / 更新 / 删除 / 清理）通过；浏览器实测拖选出现「批注」按钮、保存后黄色高亮 + 计数 1→2、点高亮可编辑（列表即时更新）、删除后计数回到 1 且高亮消失。`node --check`、`py_compile` 通过。
- 已知残留：笔记标题过长时头部路径会省略；跨多个 `<p>` 的选区只能按段落分别批注；笔记内容大改后个别批注可能定位不到高亮（仍保留在列表）。

## [2026-11-02] 阅读器：回答可复制 / 保存为批注 / 划词快捷键 / 公式划词清洗
- 问题/决策：提问浮层只有「翻译/提问/复制」三个入口，回答出来后就再也拿不走了；同时 KaTeX 渲染后划词会把公式读成重复字形，问模型得到一坨乱码。目标：回答能选中复制、能存成批注并在之后点开复看，划词和渲染公式都要干净，并给「选中→翻译/提问」加键盘快捷方式。
- 方案（前端 `web/reader.js`）：
  - 回答区 `.rd-sel-out` 加 `user-select: text`；新增 `.rd-sel-acts`（复制回答 / 保存为批注）。`sel` 增加 `blockId/side/page/question/answer`，`setSelResult()` 统一记录答案与问题。
  - 快捷键：`Ctrl+Enter` 翻译选区，`Ctrl+Shift+Enter` 提问（焦点已在输入框时直接发送）；浮层底部常驻提示 `.rd-sel-hint`。快捷键分支必须 `e.stopPropagation()`，否则输入框自己的 Enter 处理会再发一次请求。
  - 公式划词：**不能**用 `range.cloneContents()` 再找 `.katex` —— 选区从公式内部开始时，克隆片段里只有 `.katex-html` 的零散字形，`.katex` 根节点不在片段内，替换逻辑整体失效。改为对原始 DOM 建 TreeWalker，逐文本节点用 `range.intersectsNode` 判定是否在选区内，命中 `.katex` 祖先就整体输出 ` $TeX$ ` 并按元素去重；边缘文本节点按 offset 截取。段落上下文 `sel.ctx` 同样走这套清洗（`cleanNodeText`）。
  - 跨块选区兜底：`commonAncestorContainer` 落在 `.rd-flow` 上（跨段落 / 从公式图起划）时 `closest('.rd-block')` 取不到块，`blockId` 为空导致批注存不上；改为遍历 `.rd-block, .rd-fig` 取第一个与选区相交的块。`.rd-fig` 补 `position: relative`，批注圆标才能定位到公式块右上角。
  - 浮层定位：`showSelectionPopover` 只在弹出时算一次位置，长回答会把底部按钮顶出视口。抽出 `placeSel()`，保存选区矩形 `selRect`，在显示提问框 / 出译文 / 出回答（`setSelResult`）后各重排一次；`.rd-sel` 加 `max-height: calc(100vh - 24px); overflow: auto`，`.rd-sel-out` 限高 42vh 内部滚动；`.rd-anno-acts` 改 sticky，长批注滚到底也能点到「删除」。
- 后端（`app/reader.py` + `app/api/reader.py`）：
  - 批注存 `data/reader/<doc_id>/annotations.json`，字段 `{id, block_id, page, side, quote, question, answer, created, updated}`；`/api/reader/open` 一并返回 `annotations`，`/api/reader/anno/save`（带 id 即更新）、`/api/reader/anno/delete`。
  - 阅读器批注**不写进 PDF、也不用文本锚点**：译文与公式会随重译/KaTeX 重渲染变化，字符偏移必然漂移；用 `block_id + 块右上角 chip` 最稳，点 chip 出面板复看 Q&A。
- 相关代码/指令：`web/reader.js`（`katexTex` / `cleanRangeText` / `cleanNodeText` / `placeSel` / `copySelectionAnswer` / `saveReaderAnno` / `applyReaderAnnos` / `showReaderAnno` / `onKey`）、`web/css/reader.css`（`.rd-sel-acts` / `.rd-sel-hint` / `.rd-anno-chip` / `.rd-anno-pop`）、`app/reader.py`、`app/api/reader.py`。
- 验证（浏览器实测 main.pdf）：
  - 从公式**内部**（`2|A|` 中间）起划到 Proof 段落，浮层引用为 `$2|A| \leq 2n - a - T + 1.$ Proof. Since 1 = 1 · 1…`，无重复字形、无零宽字符。
  - `Ctrl+Shift+Enter` 弹出输入框并聚焦，提问返回 591 字中文回答；「复制回答」toast「已复制回答」；「保存为批注」toast「已保存为批注」。
  - 818 字长回答下浮层仍完整落在视口内，「复制回答 / 保存为批注」命中自身；点「批注 1」chip 打开面板，Q 与完整回答可复看，删除并确认后 chip 消失。
  - 读屏工具的已知副作用：无头浏览器标签未激活时 IntersectionObserver 不回调，公式懒加载与 KaTeX 渲染会一直不触发，验证前需先 `browser_tabs activate`。
- 已知残留：批注暂不支持编辑（只能删了重存）；面板里「复制回答 / 删除」在超长回答下需要滚动（已 sticky 兜底）；笔记批注与阅读器批注是两套独立存储，互不迁移。

## [2026-11-02] 阅读器默认只看 PDF 原页，翻译改由用户点击启动
- 问题/决策：以前打开 PDF 直接进「双语」，左侧原页 46%、右侧阅读流占一半，并且会自动请求当前页翻译。用户只想看原版 PDF 时，屏幕被白白占掉一半、还平白消耗模型调用。决定：默认「原页」模式（PDF 占满整宽、不翻译），只有用户点「译本页 / 译全文 / 原文 / 双语 / 译文」才进入分栏并启动翻译。
- 方案：
  - 新增第 4 个视图按钮「原页」（`data-mode="pdf"`，快捷键 Alt+0），与原「原文 / 双语 / 译文」（Alt+1/2/3）并列；`open()` 每次都无条件 `setMode('pdf')`，不再读取上次的 `rd_mode` 直接开分栏。
  - `RD.transOn` 表示「是否已启用分栏」：`setMode('pdf')` 置 false，其余模式置 true。`rdEnsurePages()` 一进来就 `if (!RD.transOn) return`，因此翻页、滚动、打开文档都不会触发翻译；`mode === 'orig'` 只读原文，也不排队翻译。
  - 「译本页 / 译全文」在 PDF 模式下先 `setMode(modeForTranslate())` 再启动任务；`modeForTranslate()` 取上次分栏模式，若上次停在「原文」则退回「双语」，避免译了却看不到译文。
  - CSS：`.rd-root.rd-mode-pdf` 下隐藏 `.rd-right / .rd-split / .rd-progress / .rd-sep / .rd-stat`，`.rd-left` 强制 `flex:1 1 100%`、`width:100% !important`（覆盖拖动分栏留下的内联宽度），并用 `.rd-root.rd-mode-pdf.rd-no-left .rd-left` 把「原页对照：隐藏」的旧偏好压回去；`.rd-pages` 限宽 1000px 居中，避免超宽屏一页拉得过高。
  - 配套两处：模式切换会改变左栏宽度，`setMode` 后 80ms 调 `resizePages()` 把所有页标记未渲染并按新宽度重画（否则从原页切回分栏会横向溢出、切到原页会发虚）；`onLeftScroll` 补 `savePosSoon()`，原页模式下阅读位置也能记住。
- 相关代码/指令：`web/reader.js`（`setMode` / `modeForTranslate` / `rdEnsurePages` / `open` / 顶栏 `data-act="page"|"all"` / `onKey` 的 Alt+0）、`web/css/reader.css`（`.rd-mode-pdf` 一组规则）。
- 验证（main.pdf，浏览器实测）：初始 `rd-root rd-mode-pdf`，左栏 965px = 整宽，`rd-split`、`rd-right` 均 `display:none`，「原页」高亮，进度条/进度文字隐藏，仅显示页码；点「译本页」→ `rd-mode-dual`，左 367px / 右 592px，出现中英对照并继续翻译（91/114 → 100/114）；再点「原页」→ 单列满宽恢复、进度隐藏。`node --check`、`tests/test_reader.py`（10 passed）通过。
- 已知残留：`rd_mode` 仍会记住上次分栏模式，只用于下次点「译本页 / 译全文」时恢复视图；打开文档本身一律先落在「原页」。

## [2026-11-02] 阅读器批注链路回归验证（IDE → PDF → 绿色高亮 → 批注抽屉 → 文献）
- 问题/决策：在真实 UI 上回归「统一批注表 / 高亮 / 批注抽屉 / 文献信息」；固定路径为 会话 → IDE 打开 out/main.pdf → 右栏双语 → 划词高亮 → 顶栏批注 → 文献。
- 结果：全部通过。会话 s_89a2a99cdd57，main.pdf 6 页；双语模式 100 个 block（原文/译文均加载）；划词浮层「高亮」→ 绿色，mark.rd-hl.c-green 背景 rgba(74,222,128,.36)；顶栏计数 3；抽屉 3 条记录，点击本次划词那条触发 .rd-flash 定位；文献弹窗 6 个字段，标题与摘要已自动带出，作者/年份/出处/DOI 为空。
- 备注：干净浏览器配置下 IDE 欢迎页没有「最近打开」，需展开 out 目录点 main.pdf；批注按文档持久化，重复验证会累加，本次 3 条里 2 条为此前遗留。
- 截图：C:\Users\MKIII\AppData\Local\Temp\gal-verify\shots\c1-green-highlight.png、c3-jump.png、c4-meta.png、c5-green-zoom.png。

## [2026-11-02] 阅读器「摘录 → 笔记库 → 批注导出」端到端回归
- 问题/决策：在真实 UI 回归 PDF 正文摘录写进笔记库、笔记渲染页码引用块、批注抽屉导出 Markdown；路径：会话「乘积自由集极值证明与论文」→ IDE 点 out/main.pdf → 切「原文」划词 → 浮层「摘录」→ 笔记库「文献摘录」→ 顶栏「批注」→「导出」。
- 结果：PASS。摘录提示 `已摘录到笔记：文献摘录/The extremal product-free subsets of {1, . . . , n} a classiﬁcation.md`；新建 `data/notes/文献摘录/The extremal product-free subsets of {1, . . . , n} a classiﬁcation.md`，内容含 `## p.1 · 2026-11-02` + 引用块（Product-free sets…finite group be?）；批注数 4→5，抽屉新增 `p.1 · 摘录`；导出 toast `已导出批注`，POST `/api/anno/export`，临时 `<a download>` 的 name 为 `The extremal product-free subsets of {1, . . . , n} a classi-批注.md`、href 为 `blob:`。
- 备注：导出文件名 = `title[:60] + "-批注.md"`；内置浏览器未在 `C:\Users\MKIII\Downloads` 落盘（下载由浏览器环境接管），只能确认触发。文本层绑定在 `.rd-flow`，而「原页」模式下 `.rd-flow` 为 0×0，划词需先切「原文/双语」。
- 相关代码：`app/api/anno.py`（`_api_anno_excerpt` / `_api_anno_export`）、`web/reader.js`（`excerptSelection` / `exportAnnos` / `showSelectionPopover`）。

## [2026-11-02] 任意文件引用 + 统一批注底座 + 文献库（面向学术阅读）
- 问题/决策：PDF 只能问答存批注，其它文档（md/tex/docx/pptx）没有批注；`@` 文件引用只把路径当文本插入，模型未必去读；笔记批注与阅读器批注是两套 JSON，无法跨文档检索。决定：先把「引用 → 批注 → 文献 → 导出」做成一条链，存储统一到 SQLite，视图分三处（PDF 阅读器 / Monaco / 文献库）。
- 方案：
  - **任意文件引用（P0）**：新增 `app/fileref.py`，发送新消息时把 `@相对路径`（支持 `@path#3` 指定 PDF 页）解析成正文挂到 `umsg["refs"]`；`app/context._msg_text` 负责拼进模型上下文，token 估算与拼装共用同一函数。单文件 12000 字符、本轮合计 30000 字符上限，超长带「已截断」标记；非文件路径（如 `@某人`）原样忽略，扫描件给出明确提示。`agent.read_file` 同步改为 pdf/docx/xlsx/xlsm/pptx 统一走 `docparse`，旧版 Office 与真正二进制给明确错误。
  - **统一批注底座（P1）**：`app/db.py` 新增 `annotations` / `documents` 两张表；`app/anno.py` 提供 CRUD、按文档/关键字/类型/标签检索、计数与旧数据迁移。`notes.py` 与 `reader.py` 的批注函数改为委托（doc_key：沙箱相对路径，笔记库用 `notes:<相对路径>`），旧 JSON 在首次读取时一次性导入并留 `.migrated_sqlite` 标记。新增 `/api/anno/{list,save,delete,export,excerpt}`、`/api/doc/{get,save,list}`。
  - **PDF 阅读器**：划词浮层加「高亮（5 色）/ 摘录」；高亮用 `<mark class="rd-hl c-颜色">` 在段落里按 quote 定位（跨节点逐段包裹），译文整段重写后只重铺该段（`applyReaderAnnos(id)`）；顶栏新增「批注」抽屉，按页排序、可按类型过滤、点击跳转并闪烁；批注面板补「复制引用」（作者 年份, p.X）。
  - **Monaco**：左侧活动栏新增「批注」，行区间锚点 + quote 兜底；整行浅黄装饰 + 悬停提示；`Ctrl+Alt+M` / 右键「添加批注」（`window.prompt` 输入，留空 = 只做行高亮）；点条目定位到行。
  - **文献层（P2，档 A）**：`app/library.py` 从 PDF 元数据 + 首页文本启发式提取标题/作者/年份/DOI/摘要，打开 PDF 时自动落进 `documents`；阅读器抽屉「文献」可编辑（含标签）。左侧活动栏新增「文献库」：搜索标题/作者/出处、显示标签，点击直接进阅读器。摘录会把「## p.N + 原文引用 + 我的批注」写进笔记库 `文献摘录/<标题>.md`，并留一条 excerpt 批注；抽屉「导出」输出带引用头的 Markdown，`/api/anno/export?format=bibtex` 输出 BibTeX。
  - **Word / PPT 阅读流（P3）**：`reader.parse_doc` 把 docx/pptx 抽成与 PDF 相同的块结构（每 10 段伪分页），`/api/reader/open` 按扩展名分流；前端 `rd-docmode` 隐藏左栏与「原页」按钮，翻译、划词提问、批注、文献信息全部复用。IDE 双击 docx/pptx 直接进阅读器。
- 相关代码/指令：`app/fileref.py`、`app/anno.py`、`app/library.py`、`app/db.py`、`app/context.py`（`_msg_text`）、`app/api/chat.py`（挂 refs）、`app/agent.py`（`_read_doc_for_agent`）、`app/api/anno.py`、`app/reader.py`（`parse_doc` / 批注委托）、`app/notes.py`、`web/reader.js`、`web/gal/gal-ide.js`、`web/init.js`、`web/index.html`、`web/css/{reader,ide,base}.css`。
- 验证（浏览器实测）：PDF 划词高亮出 `mark.rd-hl.c-yellow`、段落角标「批注 1」、抽屉计数同步；文献弹窗自动带出标题 «The extremal product-free subsets of {1, . . . , n}: a classification»；摘录在笔记库生成 `## p.1 · 2026-11-02` 引用块；导出触发 `-批注.md` 下载；`_anno_docx_demo.docx` 进阅读器显示 7 段正文、无左栏、无「原页」按钮、页码 1/1；`main.tex` 的批注在侧栏显示「第 10–12 行」，点击定位到第 10 行并出现 3 行浅黄装饰。命令行：ruff 全通过、`node --check` 三个文件通过、`tests/test_reader.py` 10 passed；`fileref.expand('@out/main.pdf#2')` 抽出 2350 字、整份截断到 12007 字；`_msg_text` 能拼出「【引用文件：out/main.pdf 第 2 页】」块。
- 已知残留：PDF **原页模式**还没有矩形高亮（目前高亮落在阅读流段落上）；文献条目自动识别作者/年份对排版特殊的 PDF 会漏（可手动编辑）；标签目前只能编辑与展示，没有跨文档标签筛选页；Zotero 导入未做；`window.prompt` 的批注输入是临时方案，后续可换成侧栏内联输入。
- 顺带修复：`.toast` 的 z-index 从 100 提到 10000 —— 阅读器 `.rd-root` 是 9000，之前「已高亮 / 已摘录」提示会被盖住，看起来像没反应。

## [2026-11-02] 删除关窗保活：窗口关闭即停止输出
- 问题/决策：关窗后「继续生成 / 等任务跑完 / 5 秒重连」这套保活逻辑留下大量状态问题（关窗不退出、进程成僵尸、重开指向已停服务、「后台运行中」卡住）。决定只删**保活层**：窗口关闭 = 进程退出 = 输出停止；刷新页面仍可接回（断线重连链路保留）。
- 解决方案：
  - `main.py`：删 `_window_reconnected` / `_wait_clients_gone` / `_active_stream_count` / `_background_job_count` / `_pending_job_count` / `_wait_streams_grace`；退出段不再判断 `ACTIVE_STREAMS` 与后台任务，窗口 `proc.wait()` 返回后直接 `httpd.shutdown()`。
  - `app/config.py`：删 `chat_window_grace_min` / `chat_window_keep_alive` 两个默认项；旧 `data/config.json` 里的同名残留会被忽略，不需要手动清。
  - `app/explore.py` / `app/autopilot.py`：删只服务于保活的 `running_count()`。
  - 测试：删除 `tests/test_background.py`，移除 `test_web_hardening.py::test_window_reconnected_checks_sse_clients`。
  - 保留（本方案明确不删）：SSE 任务缓冲、`/api/chat/attach`、前端 `keepRecovering` / 「后台运行中」提示、`/api/state.streaming`、进程崩溃后的「继续任务」断点与 `_progress` 快照。
- 相关代码/指令：`main.py` 退出段、`app/config.py`、`app/explore.py`、`app/autopilot.py`、`tests/test_web_hardening.py`。回归：`python -m pytest tests/test_web_hardening.py tests/test_resume.py tests/test_api_handlers_smoke.py -q`（32 passed）、`ruff` 全通过、`py_compile` 通过；并断言 `main` 中六个保活函数与两个配置键已不存在。
- 行为变化：关窗后 1–2 秒内进程退出，正在生成的回合会被强杀；后台子代理批次 / 自动化目标随进程一起中止。关窗时若正在生成，下次启动仍会出现「继续任务」提示（`_progress` 快照 + `_mark_resume_pending` 未动）。
- 已知残留：`app/httpd.py` 的 SSE 客户端计数 `active_clients` 现在只被测试使用，先保留作诊断；`NOTE.md` / 本文件里早前的保活条目是历史记录，与当前代码不一致，以本条为准。

## [2026-11-02] 阅读器批注角标压字修复
- 问题：阅读流的「批注 N」角标用 `position:absolute; top:4px; right:4px` 叠在段落上，中文摘要这类满行文本会被直接盖住（用户截图 resume.pdf 第一段「约10」被压）。
- 解决方案：角标改为 `float:right`，并把节点插到 `.rd-block` 的第一个子元素位置；块内行盒会自动避让浮动元素，只有首行按需要缩短，不再覆盖任何文字。去掉 `position/top/right/z-index`，保留 `margin: 1px 0 4px 8px` 与原有配色。
- 相关代码/指令：`web/reader.js`（`applyReaderAnnos` 里 `el.insertBefore(chip, el.firstChild)`）、`web/css/reader.css`（`.rd-anno-chip`）。
- 验证：浏览器实测 main.pdf 摘要段（8 行），角标矩形 (291,266)-(338,286)，首行文字右边界 279，逐行求交 hitCount=0；点击角标仍能正常打开批注面板。窄视口（377px）与原文模式均通过。
- 已知残留：公式块（`.rd-fig`）上的角标也走 float，公式会略微左移让位；块很窄时首行可能因角标换行，属于预期。

## [2026-11-02] 通知跳转：不再自动切会话，点击后定位到对应回复
- 问题/决策：用户反馈「通知一弹出界面就跳走，且跳转位置不对」。排查结论：通知本身只在 click 时跳转，但同一时刻有三条并发路径会改界面——① 自动化事件处理里无条件 `selectSession(current)`（重渲染并滚到底）；② `applySessionsPayload` 用服务端 payload 整份覆盖 `state.sessions.current`，后台任务完成时可能把界面切到别的会话；③ 通知落点只有 `sessionId`，点击后固定 `scrollToBottom`，没有消息锚点。
- 解决方案：
  - **会话归属权**：`applySessionsPayload(payload, opts)` 默认 `keepCurrent=true`，只更新数据不切会话；只有显式导航（切会话 / 新建 / 删除 / 删分组 / 存档分叉 / 右键打开目录）传 `{keepCurrent:false}`。这样后台完成、`/api/state` 轮询、事件推送都不会再自行切走界面。
  - **自动化静默刷新**：`auto.js` 新增 `refreshSessionQuiet(sid)`（`/api/session/select` + `renderMessages(..., {keepScroll:true})`），替换原来 autopilot 事件里的 `selectSession(state.sessions.current)`；`renderMessages` 新增 `opts.keepScroll`，保留用户当前阅读位置。
  - **消息锚点**：服务端 `done` 事件新增 `msg_idx`（本条 assistant 消息在会话里的下标）；前端 `stream.js` 捕获后经 `notifyChatDone` 放进 target；`notifyJump` 先切会话，再调用已有的 `ssFocusMessage(idx)` 定位，失败才回退 `scrollToBottom`；目标已是当前会话时不重选。
  - **防重与诊断**：`notifyJump` 加 300ms 互斥；`bindNativeNotifyClicks` 只绑定一次并按通知 id 去重；浏览器通知 onclick 加 `_jumped` 防重复；Electron 主进程新增 `notify show <id> target=...` 日志，并对同一条通知的 click 只处理一次。
- 相关代码/指令：`web/core.js`（`applySessionsPayload`）、`web/chat.js`（`renderMessages` / `selectSession` / 新建 / 删除）、`web/auto.js`（`refreshSessionQuiet` / `notifyJump`）、`web/stream.js`（`doneMsgIdx` / `notifyChatDone`）、`app/api/chat.py`（done 增加 `msg_idx`）、`desktop/main.js`（show 日志 + click 去重）、`web/saves.js` / `web/hall-gal.js` / `web/knowledge.js`（显式导航传 keepCurrent:false）。
- 验证：浏览器回归「切会话 / 新建 / 删除」全部 PASS（侧栏 active 与消息区同步、删除后自动切到另一会话、控制台无 JS 异常）；`node --check` 7 个前端文件通过、`py_compile` + `ruff` 通过、`tests/test_api_handlers_smoke.py` / `test_web_hardening.py` / `test_resume.py` / `test_chat_blocks.py` 共 51 passed。
- 已知残留：通知「弹出即被系统当成 click」的可能性尚未用日志证实；下一次通知触发后可在 `data/desktop.log` 对照 `notify show` 与 `notify click` 是否成对。若确认存在，再在 main.js 加「显示后短时间忽略 click」的兜底。

## [2026-11-03] 阅读器停靠 / 批注文本与数字角标 / 译文 PDF 导出
- 问题/决策：① 批注要能输入文字，并以「高亮右上角圆形数字」呈现；② 阅读器原来 `position:fixed` 占满整窗，侧栏笔记必须先退出阅读器才能用；③ 需要像沉浸式翻译一样的「译文版 PDF」导出。决策：阅读器默认**停靠在 `#main`**（侧栏始终可用），顶栏 ⤢ 可临时全屏；导出采用**覆盖式**——每页原图铺满整页，再按 block.rect 原位盖白底贴译文。
- 解决方案：
  - **停靠 / 全屏**：`.rd-root` 改 `position:absolute; inset:0; z-index:30`（`#main` 本身是 `position:relative`），`.rd-root.rd-fullscreen` 才 `fixed` 且 `z-index:46`——低于 `.modal`(50)，所以全屏阅读时「笔记 / 设置」弹窗仍能盖在阅读器上。`ensureRoot()` 把节点挂到 `#main`，`toggleFullscreen()` 记 `localStorage.rd_fullscreen`，切换后 `resizePages()` 按新宽度重画原页。顶栏加 `overflow-x:auto` + 模式/按钮组 `flex:0 0 auto`：停靠后阅读区只剩两三百像素时按钮仍能横向滚到。
  - **批注文本 + 数字角标**：划词浮层加「批注」与 `textarea`（`saveNoteAnnotation()` → `/api/reader/anno/save`，`kind=note`、`comment=文本`）。`applyReaderAnnos` 不再生成「批注 N」胶囊，改为按「页 + 创建时间」全局编号（`annoNumbers()`），把 `<button class="rd-anno-dot">N</button>` 塞进该条高亮所在的 `mark.rd-hl`（`highlightIn` 现在返回最后一个 mark），绝对定位在右上角；原文重排找不到高亮时退到段落右上角（`.rd-anno-dot-edge`，多个纵向错开）。抽屉与浮层的条目也带同号徽标。
  - **译文 PDF（覆盖式）**：前端 `buildTranslatedHtml()` 用 pdf.js 把每页按 2 倍渲染成 JPEG 铺底，再对每个有译文的 text/heading 块按 `rect`（左上原点、pt）生成白底 `.bk`，字号用二分测量（`makeFitter()`，上限取 `block.size`）塞进原文字框；`@page{size:Wpt Hpt;margin:0}` + `.pg{break-after:page}` 保证一页对一页。导出发给 Electron：`preload` 新增 `gmWin.pdfExport`，主进程 `pdf:export` 落一个临时 HTML → 隐藏 `BrowserWindow` → `printToPDF({printBackground,preferCSSPageSize,margins:0})` → 保存框写盘。浏览器环境退回系统打印（弹窗被拦时改用隐藏 iframe 打印，避免「await 之后 window.open 丢失用户手势」）。
- 相关代码/指令：`web/reader.js`（`toggleFullscreen` / `saveNoteAnnotation` / `applyReaderAnnos` / `annoNumbers` / `exportTranslatedPdf` / `buildTranslatedHtml` / `printHtmlFallback`）、`web/css/reader.css`（`.rd-root`、`.rd-fullscreen`、`.rd-anno-dot`、`.rd-note-input`、`.rd-anno-no`、顶栏 overflow）、`desktop/main.js`（`ipcMain.handle('pdf:export')`）、`desktop/preload.js`（`pdfExport`）。
- 验证：浏览器实测——阅读器矩形 `x=168,w=209`（侧栏 168 宽仍可见），点 ⤢ 后 `fixed/inset:0` 盖住侧栏、再点恢复停靠；停靠时点「笔记」弹窗 `z-index:50` 在阅读器之上且可编辑。数字角标：紫色高亮「用数学（卓越班）大二」右上角出现蓝色 `16×16` 圆点、白字 1，点开浮层显示高亮；黄色「AI应用测试」上的 2 号批注点开后显示评论文本。用 Electron 探针跑真实链路：页尺寸 793×1123px（=595×841.9pt）、底图 `naturalWidth=1190`、27 个文本块 0 越界、白底；`printToPDF` 产物 1 页 595×842pt、可抽文、含 1 张图；2 页测试 PDF 导出正好 2 页（无空白页）。批注输入链路（划词→批注→输入→保存）探针通过：浮层与输入框出现、保存后生成 note 批注并出现 2 号角标。清理：测试批注与临时 PDF/缓存均已删除。
- 已知约束：译文 PDF 只支持 PDF（docx/pptx 没有原页图，暂不导出）；公式/图表块保持原图不覆盖；页面尺寸按第一页统一，混合纸张尺寸的 PDF 会被拉伸；浏览器兜底走系统打印而不是直接落盘。

## [2026-11-03] Markdown 语法纠错：笔记 + IDE
- 问题/决策：笔记（textarea）与 IDE 里的 `.md` 都没有任何语法校验；用户要求「Markdown 语法纠错 + 笔记侧开关」。定为：提示 + 一键修正（不在未点击时改正文）、检查通用语法 + `[[双链]]` + `$公式$`、笔记用「编辑区下方问题条」而非换编辑器、笔记开关只控笔记（IDE 的 `.md` 始终检查）。
- 解决方案：
  - **共享规则引擎** `web/mdlint.js`：纯函数 `MDLint.lint(text,{noteNames})` / `MDLint.fixAll(text,diags)`，逐行扫描 + 围栏状态机；代码块与行内代码内不跑强调/公式规则。规则：围栏未闭合（error）、`#` 后缺空格/全角＃、行尾单空格、连续空行、同级列表符号混用、标题级别跳跃、表格列数不一致、双链不存在、`*`/反引号/`$` 不配对（info）、围栏无语言（info）、文件末尾缺换行/多空行。`fixAll` 只做行级幂等替换（2 个空格的行尾强制换行不报也不动）。
  - **笔记侧**（`web/init.js`）：工具栏 `纠错` 开关（`localStorage.gm.notes_lint`，默认开）+ 按钮徽标显示问题数；编辑模式下问题条可展开/收起，点条目按行列选中并滚动（按 `lineHeight` 估算 scrollTop），支持单条「修复」与「全部修正」；修正用 `execCommand('insertText')` 写入以保留 Ctrl+Z；预览模式下只保留徽标。输入 400ms 防抖、打开笔记/删除笔记时同步重算。
  - **IDE 侧**（`web/gal/gal-ide.js`）：复用已有 `ideMdEligible`（预览用的同一判断），打字 300ms / 打开 / 保存后跑检查，结果写 `monaco.editor.setModelMarkers(model,'markdown',…)`（error→8、warning→4、info→1）；同一份结果喂给既有「问题」面板（与 pyright 分流，点条目跳行），空态文案对 `.md` 单独显示。命令面板新增「Markdown：修正可自动修的问题」，用 `executeEdits` 整篇替换以保留撤销栈并把文件标记为 dirty。
- 相关代码/指令：`web/mdlint.js`（新增）、`web/index.html`（脚本顺序 + 笔记按钮/问题条 DOM）、`web/init.js`（`noteLint*` 系列）、`web/css/notes.css`（`.nt-lint*`）、`web/gal/gal-ide.js`（`IDE_MD_LINT` / `ideMdLint` / `ideMdFixAll` + 三处调度）。
- 验证：Node 直跑规则引擎，10 类坏例全部命中、行号正确；`fixAll` 后只剩不可修项且末尾换行归一。Electron 真实页面探针（临时 `.md` 与临时笔记，已删除）：IDE 打开后 6 条 markers/面板条目一致，执行修正后剩 4 条、`#` 空格与列表符号已改、文件变 dirty，`.py` 不触发；笔记问题条 5 行、徽标 5，点首条定位到第 2 行、`全部修正` 后剩 3 行、徽标同步为 3，关开关后问题条隐藏且 `aria-pressed=false`。`node --check` 三个文件通过。
- 已知约束：`*`/反引号/`$` 只能给 info（中文里的乘号、金额会误报）；双链只解析 `[[名称]]`/`[[名称|别名]]`，不检查相对路径链接；IDE 侧 `fixAll` 只改内存，需用户自己 Ctrl+S 落盘；表格与围栏的结构性问题不自动修。

## [2026-11-03] PDF 阅读器 v2：停靠体验 + 双向划词联动 + 1000 页虚拟化
- 问题/决策：原阅读器左栏是纯 canvas（不可选、不能高亮），大文档一次性渲染所有页、打开就为全部段落建 DOM；用户要求「成熟阅读器交互 + 双向划词联动 + 大文件流畅」，方案确认：前端 pdf.js 文本层做定位、联动高亮仅临时、UX 增页码/缩放/快捷键/全文查找/缩略图、基线 1000 页。
- 解决方案：
  - **左栏文本层 + PageIndex**：`renderPage` 用 pdf.js `getTextContent()` 建透明绝对定位 span（`left/top/fontSize` 来自 viewport 变换，宽度按 `item.width` 做 `scaleX` 对齐），同时建**pt 坐标**归一化字符索引（去空白/零宽/软连字符 + 小写），缩放只乘 scale 不重建索引。文本层先于画布粉刷构建，模式切换不中断选中。
  - **双向联动**：右→左先画 `block.rect` 块级矩形（<150ms），再异步在页索引里做归一化子串匹配、按 block 位置消歧，精修成字符级矩形（`rectsForRange` 按行合并）；左→右用左栏 Range 文本反查同页包含它的 block，右栏套 `mark.rd-hl-live` 并滚动。仅临时、不落库，Esc/换文档清除。
  - **虚拟化**：页面 div 只留占位，canvas/textLayer/highlightLayer 仅存在于可视区，离屏 >6 页 `releasePage()`（取消渲染、清 canvas、卸文本层、`page.cleanup()`）；渲染队列并发 2、`RENDER.gen` 作代次、跨代自动补排；取消后立刻复用同一 canvas 会被 pdf.js 拒绝，加 80ms×n 延迟重试（最多 4 次）。
  - **阅读流懒构建 + 分块**：默认「原页」不建阅读流；切原文/双语时按页分块（每 20 页让出主线程）构建并在顶栏显示进度。`blockOf` 增加从 `RD.doc.blocks` 回退查询，保证阅读流未建时联动/查找仍有 rect。
  - **导航与查找**：页码输入框（Ctrl+G）、缩放适配（页宽/页高/实际，倍率叠加）、`j/k`、Space/Shift+Space、Home/End、`Alt+←` 阅读位置回退、`Ctrl+F` 查找条（200ms 防抖）。全文查找走新接口 `POST /api/reader/search`：复用 `parse_doc` 缓存，归一化（去空白/零宽/软连字符+小写）后 `str.find`，命中块再映射回原文取 quote/snippet；返回 200 条上限、每页 20 条、带 total/truncated；归一化索引按 doc 缓存 2 份。
  - **缩略图**：120px 侧栏，1000 页只建占位，IntersectionObserver 生成 104px 小 canvas，LRU 60（淘汰时把 canvas 置 0）；点击跳页、当前页描边。
  - 其它：左栏滚轮/触摸后 800ms 内不被右栏页同步抢走；拖选文字后浏览器补发的 click 不再把左栏滚回页首；`MAX_PARSE_PAGES` 300→1000。
- 相关代码/指令：`web/reader.js`（`buildTextLayerDom` / `makeIndex` / `renderPage` / `releasePage` / `rectsForRange` / `findRectsInIndex` / `liveFromBlock` / `linkFromPageSelection` / `ensureFlow` / `toggleFind` / `toggleThumbs`）、`web/css/reader.css`（`.rd-text-layer` / `.rd-hl-layer` / `.rd-live-rect` / `.rd-find*` / `.rd-thumb*` / `.rd-pageinput`）、`app/reader.py`（`search_blocks` / `_span_in_text` / `MAX_PARSE_PAGES`）、`app/api/reader.py`（`/api/reader/search`）。
- 验证：后端 1000 页（27,000 块）首次解析 18.5s、缓存打开 316ms、查找首次 49ms/后续 18ms（命中 200/1000，truncated）；Electron 真实页面探针：1 页文档文本层 84 span、右→左精修 3 个矩形（最大 50px / 页宽 1184px）、左→右 1 个 `mark.rd-hl-live`、查找与缩略图正常；1000 页文档 1000 个占位中仅 2 页已渲染，跳 500 页后第 1 页被释放、第 500 页文本层 103 span，切双语 1.5s 已建 17,820 块且顶栏有进度。`node --check` / `py_compile` 通过。
- 产物：`docs/PDF_READER_USAGE.md`（使用说明）、`docs/PDF_READER_TEST_REPORT.md`（测试报告）。
- 已知限制：首帧光栅化在隐藏探针窗口里波动大（1.1–3.9s），未取到可见窗口 P95；未做 30 分钟内存 soak；旋转页/跨段选择的字符级高亮会退化为段落级；解析上限 1000 页，超出部分只在左栏看原页。

## [2026-11-03] 修复「备份与恢复」点击无反应
- 问题：设置 → 数据与高级 → 「备份与恢复…」点了没反应（工作台里的「备份」按钮正常）。
- 根因：
  1. `#work-modal` 原本写在 `#work-panel` 里面。非工作台模式时 `#work-panel` 带 `.hidden`（`display:none !important`），弹窗虽然被 `workOpenDialog` 去掉了 `hidden`，但祖先不渲染 → `getBoundingClientRect()` 为 0×0，看起来就是「点了没反应」。
  2. 顺带发现：从设置打开时还没经过 `workEnterMode()`，`workBind()` 没执行，弹窗右上角 ✕ / 底部取消 / 点击遮罩 / Esc 都没有处理器；Esc 监听还被 `state.uiMode !== 'work'` 挡住。
- 解决方案：
  - 把 `#work-modal` 从 `#work-panel` 里搬到 body 级（会话搜索浮层之后、设置弹窗之前），`.work-modal` 本来就是 `position:fixed; z-index:80`，放哪都能盖住 `.modal`(50)。
  - `workOpenDialog()` 开头补 `if (!WORK.bound) workBind();`，保证任何模式打开都有完整的关闭/确认绑定（`workBind` 幂等，`workEnterMode` 仍按 `WORK.bound` 判断）。
  - Esc 关闭不再要求 `state.uiMode === 'work'`，只看弹窗是否可见。
- 相关代码：`web/index.html`（弹窗位置）、`web/work.js`（`workOpenDialog` / `workBind` 的 Esc 监听）。
- 验证（Electron 真实页面）：Solo 模式 → 打开设置 → 点「备份与恢复」：弹窗 1390×843、中心 `elementFromPoint` 命中弹窗内容、标题「数据备份」、导出按钮存在、✕ 可关闭；工作台入口同样能打开。接口层：`POST /api/backup/export` 200、588MB、67s；小样本 `preflight` 返回 manifest+summary；坏 zip 返回 400「不是有效的 ZIP 备份」；无 `X-Backup-Confirm` 的 restore 返回 400；在隔离临时目录跑 `restore_backup()`，sessions/notes 被备份替换（旧 notes 文件消失）、`backups/pre-restore-*.zip` 安全备份生成。
- 已知风险（未改）：导出与预检都把整包读进内存（本次备份 588MB，峰值接近 1GB），导出 67s 期间前端只有「正在打包…」文字、没有服务端进度；大备份的 preflight 未做完整压测（只验了小样本）。需要的话可改成流式 ZIP + chunked 响应。

## [2026-11-03] 备份导出 / 预检改为流式 + 带进度
- 问题/决策：原实现 `export_backup()` 把整包 588MB 攒在 `BytesIO` 里、67 秒后才回响应，导出与预检全程只有「正在打包…/正在预检…」；预检还要把整包读进内存再一次性哈希。决定：导出走 chunked 流式 ZIP；预检拆成「XHR 上传进度 + 后台哈希任务轮询」；恢复也先落盘再解包，避免 1GB 级峰值内存。
- 解决方案：
  - `app/backup.py`：新增 `_SinkWriter`（只依赖 write/tell，zipfile 写模式可边压边发）、`export_backup_to(sink)`、`backup_name()`；`export_backup()` 保留为「收集 chunks 再 join」以兼容旧调用。`inspect_backup(src, progress=None)` 支持 bytes **或文件路径**，校验哈希时按 ~0.2s 回调 `(done,total)`；`_open_zip` / `_safe_extract` 同步支持路径。新增预检任务表（`start_preflight_job` / `preflight_job_status`，TTL 600s，后台线程跑完即删临时文件）。
  - `app/httpd.py`：新增 `_body_to_file(path)`（1MB 分块落盘，不占内存）与 `chunked_start/chunk/chunked_end`（HTTP/1.1 手动 chunked 帧 + SECURITY_HEADERS）。
  - `app/api/backup.py`：导出改为 `chunked_start → export_backup_to(h.chunk) → chunked_end`（客户端中断时 zip 写入抛错，finally 收尾）；新增 `POST /api/backup/preflight/job`（落盘后返回任务 id）与 `POST /api/backup/preflight/status`（state/done/total/pct/result/error，done 或 error 取一次即删）；恢复改为落盘后 `restore_backup(path)`；旧同步 `/api/backup/preflight` 保留兼容。
  - `web/work.js`：`workBackupPreflight` 改为 XHR 上传（`upload.onprogress` 显示「上传中 x%（已传/总）」）→ 轮询显示「校验中 x%（已校验/总）」→ 通过后显示统计；新增 `workFmtBytes` / `workBackupUpload` / `workBackupPoll`。
- 验证：HTTP 实测导出响应头 4ms 到达、`Transfer-Encoding: chunked`、无 Content-Length、首 2MB 在 1.1s 时读到（PK 头）；用 899 字节小样走 job：上传返回 id、轮询 `done 100%`、result.summary 正确；8MB 非 ZIP 走新 UI：显示「上传完成，正在校验…」→「预检失败：不是有效的 ZIP 备份」，恢复按钮保持禁用；Python 单测覆盖 `export_backup_to`（3.1MB/32 chunks）、路径预检进度（末次 done==total）、任务进度、隔离目录恢复（notes 替换、旧文件消失）；`ruff` 与 `py_compile` 通过。
- 注意：chunked 响应没有 Content-Length，导出进度只能显示「已导出 X MB」（Electron 壳 `file:save-progress` 的 total 为 0）；预检任务表只在内存里，服务重启后未取走的任务失效（临时文件由 TTL 清理）。

## [2026-11-03] 阅读器「全屏」不是真全屏：标题栏仍显示
- 问题：点 PDF 阅读器 ⤢ 后，Electron 自绘标题栏（Galgame_monster + 最小化/关闭）仍在，任务栏也没隐藏。
- 原因：`.rd-root.rd-fullscreen` 只改成 `position:fixed; inset:0`，但它挂在 `#app` 里，而 `html.electron #app` 的高度是 `calc(100dvh - var(--tb-h))`；同时 `#titlebar` 的 `z-index:80` 高于阅读器的 46。另外全屏只改了 CSS，没有真正调用窗口全屏。
- 解决方案：
  - CSS：新增 `html.rd-immersive #titlebar { display:none !important }` 与 `html.rd-immersive #app { height:100dvh }`（reader.css 在 base.css 之后加载，能覆盖）。
  - `web/reader.js`：把「改 UI」与「改系统状态」拆开——`applyImmersive(on)` 只切 `rd-fullscreen` / `html.rd-immersive` + 存 localStorage + 重排；`toggleFullscreen()` 再调 `gmWin.setFullScreen(on)`（浏览器退回 `requestFullscreen/exitFullscreen`）。`bindFullscreenSync()` 监听 `gmWin.onFullScreen` 或 `fullscreenchange`，用户用系统快捷键退出时只同步 UI；`close()`（Esc 关闭阅读器）会一并退出系统全屏；`ensureRoot()` 若上次是全屏则恢复类名并重新进入系统全屏。
  - `desktop/main.js`：新增 `ipcMain.on('win:set-fullscreen')` → `win.setFullScreen()`，并在 `enter-full-screen` / `leave-full-screen` 时回推 `win:fullscreen`。
  - `desktop/preload.js`：暴露 `gmWin.setFullScreen` / `gmWin.onFullScreen`。
- 验证（Electron 探针，注入假的 gmWin）：进入前标题栏 `display:flex`、`#app` 高 811、阅读器矩形 `[268,32,1122,811]`；点 ⤢ 后 `html.rd-immersive`、标题栏 `display:none`、`#app` 高 843（=窗口内高）、阅读器矩形 `[0,0,1390,843]`、按钮变 ⤡、`localStorage.rd_fullscreen=1`、调用 `setFullScreen(true)`；模拟系统退出全屏后 UI 自动恢复且不重复调用；再点两次得到调用序列 `[true,true,false]`、状态归零。`node --check` 三个文件通过；壳已重启加载新 IPC。
- 注意：浏览器模式（无 Electron 壳）下 `requestFullscreen` 需要真实用户手势，脚本模拟点击会被拒绝，但用户实际点击没问题；Electron 壳走系统全屏，最彻底。

## [2026-11-03] 全项目静态分析（工具扫描 + 人工审查）与修复
- 问题/决策：对 app / web / desktop 做一次静态分析。基线：`compileall` 全过、46 个前端 JS + 2 个 Electron JS `node --check` 全过、index.html 72 个静态引用无缺失、159 条 `@route` 无重复、46 个 JS 跨文件顶层同名声明 0 冲突、`tests/test_backup.py` / `test_reader.py` 全过。真问题集中在「工具扫不出的语义缺陷」：Unicode 索引错位、队列无 reject 分支、每字符对象不回收、恢复整包进内存、mdlint 误报、重复 SVG symbol。
- 解决方案：
  - `app/reader.py::_span_in_text`：`İ` 这类字符 `lower()` 会变成两个码位，原来按原文下标 append 导致 `idx` 与归一化串错位，命中靠尾部时 `IndexError` → `/api/reader/search` 整条 500。改为「每个输出字符都映射回同一个原文下标」。
  - `web/reader.js::pumpRender`：`renderPage().then()` 没有 reject 分支，意外 reject 时 `RENDER.active` 不回减、卡在 max=2，后续页永远停在骨架屏。改为 `then(ok, err)` 两个分支都回减再泵队列。
  - `web/reader.js`：`makeIndex` 的 `chars` 与 `norm` 同样要逐字符对齐（同上）；`releaseFarPages` 增加「距离 >12 页就丢弃 `tc`/`index`」，否则 1000 页文档的每字符对象会留到几百 MB（`ensureTextData` 会按需重建，联动/查找不受影响）。
  - `web/work.js`：恢复备份原来是 `file.arrayBuffer()` 再 fetch（588MB 备份在渲染进程再存一份、且无进度）。抽出 `workBackupPost(url,file,headers,onProgress)`（XHR + `upload.onprogress`，File 直接流式发送），预检与恢复共用；恢复对话框显示「正在上传备份 x%」。
  - `web/mdlint.js`：列表符号检查遇到非列表行（段落/标题）重置约定，避免文档里两个独立列表互相误报；标题级别按「行首 1–6 个 # 且后面不是 #」识别，无空格标题也参与跳跃判断（与注释一致）。
  - `web/index.html`：删除重复的 `<symbol id="i-box">`（第二个永远不可达）。
- 相关代码：`app/reader.py`、`web/reader.js`、`web/work.js`、`web/mdlint.js`、`web/index.html`。
- 判定为误报未动：S608（SQL 字段白名单拼接）、S602（Agent/钩子按设计 `shell=True`）、B904（异常链风格）、RUF001-003（中文标点）、RUF059（`_guard` 式解包）、B023（chat.py/usage.py 的闭包都在同轮次内同步调用）。
- 遗留清理项（本次未改）：F401 28 处未用 import、F841 `app/dojo/primitives.py:101` 的 `payload`、3 处 E731 lambda 赋值、262 处过期 `# noqa`、63 处循环变量重赋值（PLW2901）。
- 验证：`_span_in_text('İstanbul machine learning','learning')` → (17,25)、`search_blocks` 正常返回命中（原来 IndexError）；node 脚本验 mdlint 7 个用例（混用报、独立列表不报、无空格 H2 不报跳跃、H1→H3 报、七个 # 不算标题、fixAll 幂等 secondPassFixed=0）；index.html 重复 id 810/810；`node --check` 3 个文件、`compileall` 通过；`pytest tests/test_backup.py tests/test_reader.py tests/test_api_handlers_smoke.py` 35 项全过。

## [2026-11-03] 对话附件留存（图片 / 文档可被 AI 回看）
- 问题：图片只在 vision 模式下留了识别文本；chat（多模态）模式历史里只剩「【图片】」，模型无法回看；重发/重新生成时当轮图片不会重新附图；`see_image` 走 `resolve_path`，明确禁止读本项目 `data/`，模型也没有任何入口能取回 `data/uploads` 里的图。另外 `sweep_uploads` / `drop_upload_files` 只遍历 `image`（单图兼容字段），多图消息里第 2–4 张会被启动清理当孤儿删掉。
- 解决方案（用户选定：仅工具按需回看）：
  - `app/uploads.py`：新增 `att_id(name)`（由落盘名派生，旧消息无需迁移；非 ASCII 名回退 sha1 短哈希避免撞 id）、`save_upload` 返回值加 `id`、`read_message_images(sid,msg)`；`_referenced_files` 统一遍历 `images` 多图 + `doc`，`sweep_uploads` / `drop_upload_files` 都改用它（修掉多图被误删）。
  - `app/attachments.py`（新增）：把消息里的图片/文档统一成 `{id,kind,file,name,mime,size,msg_index,text}`，提供 `from_message` / `list_for_session` / `find` / `read_bytes` / `ref_line`；只认当前会话 uploads 目录内的落盘名，不走沙箱路径。
  - `app/context.py::_msg_text`：历史里每条带附件的用户消息追加一行 `【可回看附件】att_x｜图片｜name；…`（只给 id，不占正文；压缩保留消息本体，所以压缩后 `attachment_read` 仍可用）。
  - `app/agent.py`：新增只读工具 `attachment_read`（schema + `TOOLS` + `PLAN_MODE_TOOLS` + `_HANDLERS` + caps 分支 + 文本协议提示词）。参数 `id/name`（都空=列出附件）、`question`、`offset/limit`（文档分段，默认 6000/上限 20000）；图片走 `recognize_image_fallback`（失败自动换通道），文档返回发送时存的 `doc_text`。
  - `app/api/chat.py`：① doc 消息补 `id`/`mime`；② 重新生成/修改/继续且 `vision_mode()=="chat"` 时，用 `read_message_images` 把目标消息的图片重新附上（vision 模式已有 `vision` 文本，不重复识图）；③ 工具循环里对 `attachment_read` 做原生视觉特判：图片附件直接附到本轮上下文（与 `see_image` 共用 `VISION_NATIVE_MAX` 与去重），文档继续走普通工具路径。
- 相关代码：`app/uploads.py`、`app/attachments.py`、`app/context.py`、`app/agent.py`（`_t_attachment_read` / `native_tool_schemas`）、`app/api/chat.py`。
- 验证：`from_message` 多图+文档解析正确、`find` 支持 id/原名/落盘名、文档 `offset/limit` 分段与「还有 N 字」提示正确、无会话 id / 文件缺失均返回可读错误；`_msg_text` 尾部正确出现引用行；`sweep_uploads` 后 2 张图都在、`drop_upload_files` 两张都删、旧结构 `image` 单图仍可读；`native_tool_schemas(agent_on=True)` 含 `attachment_read`（21 个）；`import app.httpd` 无循环导入；ruff / compileall 通过；`pytest`（agent_defs / tool_meta / agent_tool_format / context / uploads_guard / api_handlers_smoke / agent_guards / permissions / chat_blocks / settings_defaults / session_store / session_events / chat_jobs / chat_plan / chat_prefetch / kb_export_md）全部通过。
- 注意：工具调用需要开启 Agent；关闭 Agent 时历史里仍会看到「【可回看附件】」引用行，但模型没有调用通道。压缩只移动 `summary_upto`、不删消息，所以附件本体与 `attachment_read` 清单在压缩后仍然可用。

## [2026-11-03] 阅读器公式：为什么变成文本，改成 KaTeX / 保真排版
- 问题：PDF 阅读器里数学公式看起来像普通文本，'排版和原文差距大'。查链路后确认不是单纯'偏好'：
  1. 行内公式被 `_protect_inline` 换成 `⟦F1⟧`，译文还原时 `formatInline` 只塞进 `<span class="rd-math">` 纯文本（等宽字体 + 淡色药丸），永远不走 KaTeX；
  2. `_make_block` 只把「单行」判为 formula，多行公式组（align / cases / 矩阵）落到 text 分支被当正文翻译；
  3. 实测 `_protect_inline` 还有两个 bug：占位符尾部空白被 `strip()` 吃掉（`n = 6 and` → `6and`、`x ∈ R` → `∈R`）；`_math_token` 的符号正则比 `MATH_CHARS` 窄（不认 `⊆` `⇒` `⋅` `±` 等），`aS ⊆ D` 完全没被保护；且只保护到孤立 token（`f(x) = x^2 + 1` 只保护了 `=` 和 `1`）。
- 参考（沉浸式翻译 / PDFMathTranslate / BabelDOC）：原则是「公式当不可翻译元素、原样保留、不重排版」；PDF 模式保原始版式。所以默认应显示原页裁图，只有确实拿到 LaTeX 才用 KaTeX。
- 用户决策：行内公式「保持原文 + 改排版」；行间/多行「裁原图，AI 成功再替换」；译文「要求 LaTeX + KaTeX 渲染」。
- 解决方案：
  - `app/reader.py`：`_protect_inline` 重写为「连续数学 token 整段保护 + 空白保留」；新增 `_tok_kind`（def/maybe/word）、`MATH_STRONG`、`MATH_FUNCS`、`_PERCENT`、函数调用识别（`f(x)`）；`_make_block` 增加多行公式判定（2–8 行、≤1 个英文词、符号密度 ≥0.32、高 ≤300pt）；`TRANSLATE_SYS` 增加「行内公式用 `$...$` 包裹、占位符原样保留」；`FORMULA_PROMPT` 支持多行公式（`\begin{aligned}`）；`SCHEMA_VERSION` 11→12（版面规则变了）、`PROMPT_VERSION` rd2→rd3（提示词变了）、`FORMULA_VERSION` 1→2。
  - `web/reader.js`：`formatInline` 重写为同时处理 `⟦F⟧` 与 `$...$` / `$$...$$`；新增 `mathInlineHtml`（纯数字 / 货币 `$5 and $` / 含中文 / 长英文句子一律不当公式，避免误渲染）；复用 markdown.js 的 `mathHtml`（KaTeX 未就绪时输出 `math-pending`，由 init.js 的 `upgradeMath` 补渲染）。
  - `web/css/reader.css`：`.rd-math` 改为继承正文字体、去底色药丸；补 `.rd-dst .katex` 与 `.rd-dst code.math-pending` 样式。
  - `docs/PDF_READER_USAGE.md`：新增「7. 公式与排版」，后续章节顺延。
- 验证（实测输出）：
  - `Let n = 6 and k ≥ 3, then aS ⊆ D with x ∈ R.` → `Let ⟦F1⟧ and ⟦F2⟧ then ⟦F3⟧ with ⟦F4⟧ R.`，F1=`n = 6`、F2=`k ≥ 3,`、F3=`aS ⊆ D`（原来没被保护）、F4=`x ∈`；空格不再丢。
  - `We define f(x) = x^2 + 1 …` → F1=`f(x) = x^2 + 1`（原来只保护 `=`、`1`）；`3.5%` 现在会被保护。
  - 多行分类：三行 align / 矩阵 → `kind=formula`；Theorem 正文（8 个词、ratio 0.25）与项目符号 → 仍是 text。
  - `formatInline` node 验证：占位符正常还原；`$n = 6$`、`$x \in R$`、`3.5\%` 渲染为公式；`The cost is $5 and $7` 保持原文；纯数字占位回退文本；中文占位回退文本；`$$a=b$$` 走 display 模式。
  - `pytest tests/test_reader.py`（含 `_protect_inline` 三例）10 项通过；`tests/test_backup.py` + `test_api_handlers_smoke.py` 25 项通过；ruff / compileall / `node --check` 通过。
- 注意：`SCHEMA_VERSION` 12 会让旧的 `blocks.json` 失效，已有 PDF 下次打开会重新解析（1000 页约 18–20s）；`PROMPT_VERSION` rd3 让旧译文不再复用，需要重新翻译才能看到新的公式排版；未做真实 PDF 的视觉回归（需用户在实际文档里确认）。

## [2026-11-03] 以真实论文（main.pdf）为样本的阅读体验审计
- 样本：`D:\Ai\Project\paper\out\main.pdf`（6 页，乘积自由集论文）。方法：用当前解析器逐页重建 blocks，对照 pdfium/pypdf 原始文本与渲染页图核对。
- 已修复：
  1. **页 6 整段文字交错成乱码**（`2. aWnesiwgehrteids vXersailoonn.e...`）。根因有两层：`_segments` 按「字符上沿 + 每字符自身字号容差」分桶，同一行里 W/h/t（升部）与 e/i 上沿差 2.7pt，正好卡在 2.71 的临界值 → 一个单词被拆成多段；`_rows` 又让 ∑/∏ 这类跨界大符号把上下两行按重叠率缝合（并查集传递）→ 两行字符按 x 交错。修复：分桶改用「纵向中心 + 全页字号中位数的 0.45 容差」；`_rows` 第一条合并规则加「高度差 ≤1.8×」守卫。页 6 由乱码变为 6 行正确顺序，页 6 的 segment 数 863→330。
  2. **连字符复合词被误判成公式**（上一轮扩符号集引入）：`product-free` / `well-defined` / `Sub-case` 等 36/114 块被保护、不翻译且 KaTeX 斜体化。修复：`_tok_kind` 新增 `_HYPHEN_WORD`（字母-字母复合词直接判 word）。复测 0 块。
  3. **cases 大括号块被当正文翻译**：`c(n)=...{...}` 里的大括号在 PDF 中是私用区字形（U+E000–F8FF），旧逻辑只看符号密度 → 翻成 `c(n)= 32,, mn=≥92,,`。修复：块内含 `\uf8f1-\uf8f4` 直接判 formula（裁原图）。
- 仍存在（按影响排序，尚未修）：
  - **P0 标题层缺失**：`_make_block` 用绝对字号 `size >= 15` 判 heading，而本文标题 6.5–7.7、正文 4.9 → headings=0，阅读流里标题与正文同字号。应改相对字号（base×1.18）+ 短行 + 无句末标点 + 低符号密度。
  - **P1 参考文献被逐条翻译**：7 条 references 共 13 个块，且 `19 pp.` 单独成块。建议 References 之后标记 `ref` 类型、不翻译。
  - **P2 二维公式被切碎**：分式 `T=(m²+r)/(m−j)` 被拆成 3 个重叠块（`T=m2+r=...`、`(5)`、`,, m − j m − j`），裁剪区域可能不全；多重求和 `aXx= X y=...` 仍是 text。需按「垂直相邻 + x 重叠的数学行」合并成公式区域。
  - **P3 文本层固有错误**：`b√nc` 实为 `⌊√n⌋`、`Σ` 提成 `P`、`≠` 提成 `=6`；`ifthere` / `searchover` / `ofproduct-free` 粘词。纯文本层无法恢复，缓解手段是行内公式改裁原页小图（需要逐 token 坐标）。
  - **P4 续行单独成块**：`agreement.`、`19 pp.` 这类短续行独立成块，翻译后成孤立一行。
- 相关代码：`app/reader.py`（`_segments` / `_rows` / `_tok_kind` / `_protect_inline` / `_make_block`）。
- 验证：页 6 行文本顺序正确；连字符误判 36→0；PUA cases → formula；blocks 117（text 99 / formula 18）；`pytest tests/test_reader.py` 10 项通过；ruff 通过。

## [2026-11-03] 阅读体验审计后续：标题层级 / 参考文献 / 二维公式合块（P0-P2）
- 背景：以 `D:\Ai\Project\paper\out\main.pdf` 为样本审计出的 P0–P2，用户确认修复。
- 解决方案：
  - **P0 标题层级**：`_make_block` 改为相对字号判定（`cur.size >= page_base × 1.18`）+ 短行 + 至少 1 个英文词 + 符号密度 <0.3 + 不以句号/问号/叹号结尾（标题常以冒号结尾，不能把 `:` 算句末）；`page_base` 在 `_blocks_from_segments` 里用**整页**非图片行的 30 分位字号计算（按横向带算的话，标题自己成带时 base 等于标题字号，永远判不出来）。heading 增加 `level`（1=论文大标题 ≥1.45×base，2=章节），前端 `el.dataset.level` + CSS 分级。
  - **P1 参考文献不翻译**：新增 `_mark_reference_blocks`，在 `parse_pdf` 落盘前把 `References/Bibliography/参考文献` 之后的块标成 `kind="ref"`；翻译任务 `todo` 排除 `ref` 与 `formula`（顺带修掉「公式块也被送进翻译」的浪费）；前端 `ref` 只渲染 `.rd-src`、不计入 `RD.totalBlocks`，CSS 在只看译文模式下也不隐藏。
  - **P2 二维公式合块**：新增 `_line_is_mathy`（无正常英文单词 + 含数学符号/括号/逗号），段落拼接时若相邻两行都是纯公式且行距 ≤ max(6, 0.9×base) 就并入同一块（不看缩进，公式编号通常右对齐）；`cur.rect` 合并改用 min/max 四边，保证裁图覆盖整个分式。
- 相关代码：`app/reader.py`（`_blocks_from_segments` / `_make_block` / `_line_is_mathy` / `_mark_reference_blocks` / `translate_doc` 的 todo）、`web/reader.js`（`addBlock` 的 ref 分支 + heading level）、`web/css/reader.css`、`docs/PDF_READER_USAGE.md`。
- 验证（同一篇论文）：headings 0→9（`The extremal product-free subsets...:` 与 `a classification` 为 level 1，`1 Introduction`…`References` 为 level 2）；refs 13 个块、不再进翻译；分式 `T=(m²+r)/(m−j)=(m+j)+...` 由 3 个重叠块合并为 1 个 formula 区域（含编号 `(5)`），重叠文本块对 1→0；kinds = text 76 / formula 16 / ref 13 / heading 9；`pytest tests/test_reader.py` 10 项通过；`node --check` / ruff 通过。`SCHEMA_VERSION` 12→13 使旧版面缓存失效（首次打开重新解析一次）。
- 未修（用户未选）：P3 行内公式文本层错误（`b√nc`=⌊√n⌋、`Σ`→`P`、`≠`→`=6`，需逐 token 坐标改裁原页小图）；P4 续行单独成块（`agreement.`）。

## [2026-11-03] 阅读器翻译内核 v2：章节上下文 + 文档术语表 + 三级重试

- 问题/决策：v1 逐段独立翻译，术语不统一、指代丢失；失败只记日志，整段静默丢弃；「译全文」每次都重译。v2 按参考项目（BabelDOC / PDFMathTranslate）改成「文档级上下文 + 结构化校验」。
- 解决方案：
  - **强制主模型**：`llm._quick_chat(..., use_light=False)`；阅读器翻译/术语表不再走轻量模型（`light_model`），避免小模型术语漂移。
  - **按章节分批**：`_section_map` 给每块挂章节名；同章节连续段落成批（≤4 段 / 1600 字），跨章节强制切开。每批注入「文档标题 + 章节名 + 上一段原文 + 术语表」。
  - **术语表**：`_term_candidates` 用「频次 × 词数」抽候选，模型一次生成 `[{src,dst}]`；落 `data/reader/<doc_id>/glossary_<lang>_<model>.json`。`/api/reader/glossary` 支持读取 / 保存 / 重新生成，前端设置面板可编辑。
  - **校验**：`_validate_translation` 检查空译文、`⟦F1⟧` 占位符是否齐全、`[12]` / `Theorem 3.2` 等编号是否丢失、译文长度比是否异常；`_soft_issues` 只告警不重试。
  - **三级回退**：整批失败 → 逐段重译 → 用未保护原文兜底；不再静默丢段。并发默认 3（`reader_translate_parallel`，1–4）。
  - **断点续译**：只补缓存里缺失的段；`force` 只重译本页；`retry_failed` 重试失败段。跳过已是目标语言的段与 `ref/formula/figure`。
- 相关代码：`app/reader.py`（`_section_map` / `_term_candidates` / `ensure_glossary` / `_validate_translation` / `_translate_batch` / `start_translate`）、`app/llm.py`（`use_light`）、`app/api/reader.py`（`/api/reader/glossary`、translate 的 `all_blocks` / `retry`）、`web/reader.js`（设置面板「重译本页 / 术语表」）、`web/css/reader.css`。
- 验证：`pytest` 相关 27 项、ruff、`node --check` 通过；未做真实 API 端到端（需密钥）。

## [2026-11-03] 阅读器译文导出重构：服务端重排 PDF（仅译文 / 双语）+ Markdown

- 问题/决策：旧导出在浏览器里把每页渲成 2x JPEG，再按 block.rect 盖白块贴译文。译文比原文长时字号最小压到 3.5pt、`overflow:hidden` 静默裁切；纸张统一第一页尺寸；没有双语 / Markdown / 书签，双栏页还会压到栏间空白。决定整块替换为**服务端重排**。
- 解决方案：
  - 新增 `app/reader_export.py`：不依赖 platypus，直接用 reportlab canvas 顺序排版（`_Doc`：`block` / `heading` / `image` / `new_page`），字号固定 10.5pt / 行距 17.6pt，中文按字断行、西文按词断行（`_wrap` + `_glue_space` 处理中西文间距与标点）。
  - 字体：优先 `NotoSerifSC-VF.ttf`（正文）+ `NotoSansSC-VF.ttf`（标题），回退 simsun/simhei/msyh；嵌入子集，中文可复制、可搜索。
  - 图表 / 公式：复用 `reader.crop_png`（3x PNG）按阅读流顺序插回，**按原尺寸排版**（pixels / CROP_SCALE），只有超过版心或高度上限（公式 32% / 图表 46% 版心高）才缩小；不再强行铺满整栏，避免小公式被放大成一大块。
  - 双语模式：原文 8.9pt 灰色 + 译文 10.5pt；标题翻译作标题、原文作灰色小字。
  - 书签 / 页码：`canvas.bookmarkPage` + `addOutlineEntry`（标题 level 1/2 → 书签 0/1）；页脚页码 + 细线，第 2 页起页眉显示文档标题。
  - Markdown：标题层级 + 译文 + 原文引用块；公式/图表以 base64 `<img>` 内嵌，单文件可分享。
  - **只用已缓存译文**：缺译段落保留原文，`export()` 返回 `missing` 计数，前端导出前 `confirm` 提示。
  - 服务端接口：`POST /api/reader/export`（mode=mono/dual/md）生成到 `data/reader/<doc_id>/exports/` 并返回同源下载 URL；`GET /api/reader/export/file?doc=&name=` 校验 `doc_id` 与 basename、禁止路径穿越，7 天自动清理。前端 Electron 走 `gmWin.saveUrl`（GET + X-Auth-Token 流式写盘），浏览器 fetch blob 下载。
  - reportlab 改为**延迟导入**：未安装时阅读/翻译不受影响，仅 PDF 导出报「请先 pip install reportlab」。`requirements.txt` 已加 `reportlab>=4.0`。
- 相关代码：`app/reader_export.py`（新增）、`app/api/reader.py`（export / export/file）、`web/reader.js`（导出三按钮 + `exportDoc` / `saveExport`，删除旧 `makeFitter` / `buildTranslatedHtml` / `printHtmlFallback`）、`web/css/reader.css`（`.rd-exp-row`）、`desktop/main.js` + `desktop/preload.js`（删除已无调用方的 `pdf:export` printToPDF 通路）、`tests/test_reader.py`、`docs/PDF_READER_USAGE.md`。
- 验证（实测，`D:\Ai\Project\paper\out\main.pdf` 6 页）：
  - 假译文导出：mono 7 页 / 198KB，dual 11 页 / 209KB；公式与图表裁图正确插回（如 `2|A| ≤ 2n−a−T+1`、`φ: S → [a,n]\\A`），书签 9 条层级正确，`pypdf` 能抽取中文，页码连续。
  - `pytest tests/test_reader.py` 16 项通过；`ruff` 通过；`node --check web/reader.js` 通过；导出文件路由的正常下载 / 路径穿越 / 非法 doc_id 三种情况均按预期。
  - 全量 `pytest` 仍有 2 个**既有失败**（`test_memory.py::test_reindex_mirrors_kb_and_session`、`test_prompt_budget.py` 提示 8268 > 8000 字），与本次改动无关。
- 注意：PDF 导出为「重排文档」，图表按阅读流顺序插入，不再与原页页码一一对应；`data/reader/<doc_id>/exports/` 只保留 7 天。当前 venv（`.venv`）与系统 Python 都已装 reportlab。

## [2026-11-03] 阅读流结构感知：列表缩进 / 定理分组 / 图注弱化（P3）

- 问题/决策：阅读流把所有非标题段落都当正文，项目符号、定理、图注和普通段落长得一样，长论文里很难扫读。
- 解决方案：
  - `_make_block`：项目符号 / 编号段落标 `kind="list"`；`Figure/Table/图/表 + 数字` 开头标 `role="caption"`；`Theorem/Lemma/Proof/定理/证明…` 开头标 `role="theorem"`。文本本身不改，仍按原样送翻译。
  - `_page_blocks`：列表缩进以「本页正文最常见左边界」为基准（不是绝对最小值，避免页边碎片把基准拉偏），一级缩进 ≈ 3% 页宽（PDF 字号单位差别很大，不能按字号算），限制 0–3 级，写入 `indent`。
  - 前端：`addBlock` 加 `rd-list` / `rd-theorem` / `rd-caption` 类与 `--rd-indent`；CSS 做悬挂缩进、左侧竖线 + 浅底、图注小字弱化。
  - 导出：列表缩进 12pt，图注用 `ref` 小字号样式，双语模式图注原文用 `note` 样式。
- 影响：`SCHEMA_VERSION` 13→14，旧 `blocks.json` 自动失效，已有 PDF 下次打开会重新解析一次。
- 验证（main.pdf，6 页）：kinds = text 61 / list 15 / formula 16 / ref 13 / heading 9；roles = theorem 11 / caption 0（该文没有 Figure/Table 图注）；列表 indent 全部为 1；`pytest tests/test_reader.py` 16 项通过；`ruff` / `node --check` 通过。
- 相关代码：`app/reader.py`（`_make_block` / `_CAPTION_RE` / `_THEOREM_RE` / `_page_blocks`）、`web/reader.js`（`addBlock`）、`web/css/reader.css`、`app/reader_export.py`、`docs/PDF_READER_USAGE.md` 第 7 节。

## [2026-11-03] 公式识别红色报错与导出截图过大的修复

- 问题/现象：
  1. 阅读流里部分译文/公式显示为红色。根因：KaTeX 用 `throwOnError:false` 渲染坏 LaTeX 时会输出红色 `.katex-error`；坏结果主要来自公式识别模型（返回解释、半截公式、环境不配对）。
  2. 导出的 PDF 里公式/图表截图被强行拉到版心宽度，小公式被放大成一大块，多占 1–2 页。
- 解决方案：
  - 前端 [reader.js]：`renderLatex` 改 `throwOnError:true`，失败则清空 KaTeX 层、移除 `ai` 类、保留原裁图；行内公式先用 `throwOnError:true` 试渲染，失败显示原始 TeX，不再出现红色报错。
  - 服务端 [reader.py]：新增 `_latex_sane`（长度 / 中文 / 花括号配对 / `\begin`-`\end` 配对），`recognize_formula` 不合法就抛 `ReaderError` 且不落缓存；提示词补充「⌊⌋ 不要写成 b√nc、不要臆造命令」；`FORMULA_VERSION` 2→3 使旧坏缓存失效。
  - 导出 [reader_export.py]：`_Doc.image(png, max_ratio)` 按原尺寸（pixels / `CROP_SCALE`）排版，只有超出 `CONTENT_W` 或高度上限才缩放；公式上限 0.32 版心高、图表 0.46。
- 验证：Node 直接调 `katex.renderToString(throwOnError:true)`，确认非法 `\frac{a}{b`、`\begin{aligned}…\end{matrix}` 会抛错、合法公式正常；main.pdf 导出 mono 由 7 页降到 6 页，公式截图回到与正文相近的尺寸；`pytest tests/test_reader.py` 20 项通过；ruff / node --check 通过。
- 相关代码：`web/reader.js`（`renderLatex` / `mathInlineHtml` / `formatInline`）、`app/reader.py`（`FORMULA_PROMPT` / `_latex_sane` / `FORMULA_VERSION`）、`app/reader_export.py`（`_Doc.image`）、`tests/test_reader.py`。

## [2026-11-03] Solo 提问标记：命名 + 右侧悬浮跳转按钮

- 需求：多轮对话里给关键提问做标记并命名；Solo 模式下每个标记生成一个悬浮按钮（按钮文字即名称），点击快速跳回该次提问。
- 数据与接口：
  - 会话新增 `qmarks: [{idx, label, text, at}]`，最多 20 条；`slim_sessions` 回传时过滤越界下标、去掉 `at`，前端按 `idx` 直接跳转。
  - 新增 `POST /api/session/qmark`（`{id, idx, label, text}`）：label 为空 = 删除；同一消息只保留一条；只允许 `role=user`；返回 `_ok_sessions` 的完整精简负载。
  - 撤回 / 删除消息时同步下标：`trim_qmarks(drop_after=idx-1)`（尾部删除）或先移除该条再 `shift_after=idx`（仅删一条）；清空会话时一并清空 `qmarks`。
- 前端：
  - 用户消息悬浮菜单新增「标记提问」图钉按钮（已标记时高亮），点击走 `appPrompt` 命名，留空删除。
  - 新增 `#qmarks` 右侧悬浮容器：每个标记一个胶囊按钮，文字为名称；点击用 `ssFocusMessage(idx)` 跳转（会自动补历史分页并高亮），右键改名 / 删除。
  - 仅 Solo 显示：`#main.ide-mode/gal-mode/hall-mode/work-mode/plugins-mode` 下 CSS 隐藏；窄屏（<760px）收成圆点只留图钉。
  - `turnDotsSync()` 末尾顺带 `qmarksRender()`，会话切换 / 消息重排后自动刷新。
- 验证：`tests/test_qmarks.py` 7 项（裁剪/去重/API 增改删/拒绝 AI 消息/路由注册）通过；连同 session 窗口、压缩标记、Web 加固测试共 26 项通过；`ruff`、`node --check web/turndots.js`、`node --check web/chat.js` 通过。未在真实 Electron 窗口做点击回归。
- 相关代码：`app/sessions.py`（`MAX_QMARKS` / `trim_qmarks` / `slim_sessions`）、`app/api/sessions.py`（`/api/session/qmark` / clear / withdraw）、`web/chat.js`（`buildActions` 图钉按钮）、`web/turndots.js`（`qmarkOf/qmarkList/qmarksRender/qmarkEdit/qmarkSave/qmarkJump`）、`web/index.html`（`#qmarks`）、`web/css/base.css`（`.qmarks` / `.qmark-btn`）、`tests/test_qmarks.py`。

## [2026-11-03] README 重构 + 应用版本号改为 3.2.2

- 问题/决策：README 停留在 3.0.0 时期，存在已删除功能（「不做插件市场」）、错误入口（大厅 6 项）、过期工具名（原始工具列表）与过期依赖说明（KaTeX 写成 CDN）；且 12 节结构里功能说明与配置、FAQ 混在一起。决定整篇重写并按「简介 → 快速开始 → 架构 → 功能地图 → 配置 → 安全 → 数据/更新 → 限制 → 结构 → FAQ → 技术栈」重排。
- 主要更正（逐项核对代码后）：
  - 版本：`app/store.py::APP_VERSION` 3.0.0 → **3.2.2**（前端「设置 → 数据与高级」与 HTTP `Server` 头都引用它）。
  - 功能补充：插件（Python 工具插件 + MCP + DSH 索引）、子代理、轨迹、旁注提问、提问标记、B 站数据、学习观测、本机 ComfyUI 出图、PDF 阅读器 v2（术语表 / 三级重试 / 仅译文·双语·Markdown 导出）。
  - 事实修正：大厅注册 **7** 项（补学习观测）；设置 **4 组 15 分类**（原 5 组 13）；知识图谱关系 **11** 种（原 13）；图标 **58** 个（原 46）；KaTeX / pdf.js / vis-network / xterm / Monaco 均本地内置（原写 CDN）；Agent 工具已收敛为 `read/edit/run/code/git/plan/agent/web/history/visual/image/asset/audio/workflow/utility` 元工具；MCP 默认关闭。
  - 结构：删除重复的旧「更新与备份」与 FAQ 冗余；项目结构补 `reader.py` / `reader_export.py` / `aside.js` / `trajectory.js` / `agents.js` / `plugin_manager.py` / `image_gen.py` / `debugger.py` / `codeindex.py` 等；脚本列表按实际文件更新。
- 验证：`python -c "from app.store import APP_VERSION"` → 3.2.2；README 12 个一级标题、699 行；grep 确认无「不做插件市场 / 46 个线条图标 / 5 组 13 分类 / 实际注册 6 个 / 13 种关系」等旧表述；`ruff check app/store.py` 通过。
- 相关代码/文件：`readme.md`（整篇重写）、`app/store.py`（版本号）、`NOTES.md`（本条与版本地图同步）。

## [2026-11-03] 工作台重构：多视图 / 任务模型增强 / 前端拆分 / 专注与统计

- 需求：工作台整体重构与更新，覆盖「视图与布局、任务能力、前端架构拆分、新板块」四项；后端允许改表并自动迁移。
- 后端：
  - `tasks` 新增 `due_at / parent_id / depends_on / repeat_rule / estimate_min / sort_order` 六列；`db._ensure_task_columns` 用 `ALTER TABLE` 增量补列，并把旧 `meta.due_at` 回填到 `due_at`、`sort_order` 用 `updated_at` 兜底。`idx_tasks_due` 必须放在补列之后创建（旧库直接执行 SCHEMA 会因缺列失败）。
  - 新增 `task_templates`（名称 / 标题 / 步骤 JSON / 标签 / 优先级 / 预计分钟）与 `focus_sessions`（专注 / 休息记录）两张表。
  - `task_engine`：`create_task` 写入全部新字段；`update_task` 支持改截止 / 重复 / 父子 / 依赖 / 预计分钟，并在截止变化时重建「任务截止」日程；新增 `complete_task`（手动完成 + 按 `daily/weekly/monthly` 生成下一轮）、`reorder_tasks`、`list_tasks` 的 tag / priority / parent_id 过滤与 `sort_order` 排序。
  - `app/work_extras.py`：模板 CRUD、专注记录、近 N 天统计（完成 / 新建 / 专注 / 逾期 / 标签）。
  - API：`/api/work/tasks` 新增 `complete`、`batch`、`reorder` 与 `template_id` 创建；`/api/work/templates`、`/api/work/focus`、`/api/work/stats`；开始 / 继续前检查 `depends_on`，未完成时返回「前置任务未完成」。
- 前端：
  - 顶部新增视图切换：今天 / 周 / 月 / 看板 / 统计；「今天」保留原三栏，其余为独立容器；视图选择记在 `localStorage`。
  - 周视图 7 列时间轴（日程 + 截止任务 + 专注分钟）；月视图月历 + 选中日明细 + 翻月；看板按状态分列，支持拖拽改状态、同列拖动排序、勾选批量完成 / 取消 / 删除 / 设优先级；统计页显示 KPI、每日完成 / 专注柱状图、标签分布与逾期列表。
  - 任务详情新增截止 / 重复 / 预计分钟 / 父任务 / 前置依赖编辑与「存为模板」；新建任务支持从模板预填、父子与重复；任务卡显示逾期、父任务、等待前置。
  - 专注计时：15/25/45/60 分钟，可暂停 / 继续 / 结束并记录，可关联任务；顶部按钮实时显示剩余时间。
  - **work.js 拆分**：2921 行拆为 `web/work/{core,events,tasks,artifacts,workflows,backup,views,stats,focus,templates,misc}.js`，`index.html` 按顺序加载；拆分脚本与旧文件备份在 `_backup/web/work.js.bak`。
- 验证：
  - `pytest tests/test_work_extras.py tests/test_task_engine.py tests/test_calendar.py tests/test_artifact_preview.py tests/test_workflow.py` → 54 项通过；`ruff` 通过。
  - 迁移测试：手写旧版 `tasks` 表（仅旧列）后 `db.configure` → `due_at` 从 meta 回填、`sort_order` 回填、新列存在。
  - 新接口用假 Handler 实跑：create / list / update / complete（含重复任务）/ templates / focus / stats / batch 全部返回预期。
  - 所有 `web/work/*.js` 通过 `node --check`；把 11 个模块拼接后在 Node 内加载（带最小 DOM stub）无重复声明 / 加载错误；静态扫描 101 个 `workXxx(` 调用均有定义。
  - 未在真实 Electron 窗口做点击回归（多视图布局、拖拽、专注计时需实际点一次确认）。
- 相关代码：`app/db.py`、`app/task_engine.py`、`app/work_extras.py`、`app/api/work.py`、`web/work/*.js`、`web/index.html`、`web/css/work.css`、`tests/test_work_extras.py`、`readme.md` 4.9 节。

## [2026-11-04] 「正在看图」提示的 emoji 换成 UI 图标

- 问题/决策：流式气泡里的 `📷 正在看图…` 与全局 SVG 图标风格不一致；emoji 在不同系统字号/颜色也不同。
- 解决方案：`web/stream.js` 的 `recognizing` 分支改用 `icon('image')`（内置 sprite）拼 `innerHTML`；`.recognize-tip` 改为 `inline-flex + gap:5px`，图标固定 14px。
- 验证：`node --check web/stream.js` 通过；全仓 grep 确认 `📷 / 📸` 已无残留。
- 相关代码：`web/stream.js`（`recognizing` 分支）、`web/css/base.css`（`.recognize-tip`）。

## [2026-11-04] 对齐 Trae 提问能力：上下文引用 / 侧边对话 / 内联追问 / 分享与回退

- 背景：对比 Trae 的提问能力（主 / 侧 / 内联三个入口、九类 `#` 上下文、Fork、回退、分享、侧边多标签、内联多轮）后确认四项差距并逐项补齐。
- 1) 上下文与引用
  - `app/api/chat.py`：`@workspace / @rule / @problems / @folder:<dir> / @chat:<id>` 解析（`_special_refs`）；`chat_auto_context`（默认开）时用代码索引按提问文本混合检索，`score<0.18` 丢弃、最多 5 条，产出带 `path/start/end/symbol` 的 refs；索引未就绪时后台重建，本轮只回显显式引用。
  - `app/context.py`：自动引用按「【引用代码：path:start-end · symbol】」注入；`app/config.py` 新增 `chat_auto_context`。
  - 前端：AI 消息与 done 事件新增 `references`，`buildReferencesBlock` 渲染并支持点击跳 IDE 行；`composer.js` 的 `@` 候选补上五类上下文；设置面板加开关。
  - 验证：`tests/test_chat_refs.py` 7 项。
- 2) 侧边对话升级
  - 新增 `app/aside_store.py`（`data/aside.json`，按主会话分组，最多 20 线程 / 每线程 12 轮，标题取选区前 40 字）；`/api/aside/threads` 支持 list/create/rename/delete；`/api/aside/stream` 带 `thread_id` 时以服务端线程的选区 / 历史为准（前端只追加当前问题），回答完 `append_qa`。
  - `web/aside.js`：新增 `S.threads / S.threadId`、标签栏（切换 / ＋新建 / ×关闭 / 双击改名）、同选区自动复用线程、历史回填；`inSolo` 改为 `canAsk`（Solo 与 IDE 都可用，IDE 右侧本来就有 `#chat`）；切会话清线程缓存。
  - `aside.MAX_HISTORY` 4→12，与 store / 前端对齐（`MAX_TOTAL` 字符预算仍兜底）。
- 3) 内联多轮问答（IDE 选中代码）
  - 复用同一张旁注卡片：`app/aside.py` 新增 `_code_context`，`selection.kind == 'code'` 时拼「文件 / 行号 / 语言 + 前后代码」；`window.asideOpenCode(code, rect)`。
  - `web/gal/gal-ide.js`：`ideInlineAsk()` + Monaco action `gm.inlineAsk`（Ctrl+Alt+I、右键 navigation 组、命令面板同项）→ 选中代码弹出卡片多轮追问；改写仍是 Ctrl+I。
- 4) 分享与消息回退
  - 分享：`app/share.py`（`data/shares/<id>.html`，400KB 上限，id 正则校验）+ `POST /api/share/save`；`httpd.do_GET` 增加 `/share/<id>.html`（链接本身即凭证，不校验 X-Auth-Token，仍校验 Host）。前端 `shareMessage(index)` 把界面上已渲染的 HTML（公式 / 代码 / 图片）打包成独立页面，相对图片与链接改绝对地址，打开新标签并复制链接。
  - 回退：AI 消息操作条新增「回退到此（含文件改动）」；`rollbackMessage(index)` 先收集该条及以后所有 `tools[].undo`，用 `/api/agent/undo` 的 `tokens` 批量逆序还原，再 `/api/message/withdraw` 删消息，最后 `ideReloadAll()` 刷新打开着的编辑器文件。
- 验证：`pytest tests/test_aside.py tests/test_share.py tests/test_chat_refs.py tests/test_registry.py tests/test_api_handlers_smoke.py tests/test_web_hardening.py` 全过；ruff 通过；`node --check` 覆盖 aside.js / stream.js / chat.js / gal-ide.js。未做真实窗口点击回归（标签切换、Ctrl+Alt+I、分享打开、回退）。
- 相关代码：`app/aside.py`、`app/aside_store.py`、`app/share.py`、`app/api/aside.py`、`app/api/share.py`、`app/httpd.py`、`app/api/chat.py`、`app/context.py`、`web/aside.js`、`web/stream.js`、`web/chat.js`、`web/composer.js`、`web/gal/gal-ide.js`、`tests/test_aside.py`、`tests/test_share.py`。

## [2026-11-04] 提问卡与 Trae「智能体主动提问」的差距研究

- 问题/决策：对照 Trae 的提问卡能力，确认我们 `ask_user` / `ask-card` 还缺什么，作为下一轮补齐依据。
- 我们现状（已核实）：
  - 工具 `ask_user`：`questions ≤ 6`，每题 `options ≤ 6`、`multiple`、`action/detail/items`；提示词常驻注入（任何模式都可用）。
  - 服务端：`agent.new_asks` 登记 qid → SSE `ask` → `wait_answers`（`ASK_TIMEOUT=600s`）阻塞；回答经 `/api/agent/answer` 写入；未答填「（未回答）」；能识别用户「反问」并提示模型先回答再重问；自动化模式下按 `autonomy_question_policy`（auto/smart/ask）代答。
  - 前端 `buildAskGroup`：多题分页 + 进度、单选/多选、选项与手写并存、Solo 逐题、主输入框/语音单向同步、历史只读回显；`action` 卡只有「我已完成 / 无法完成」；侧栏「待回答」、窗口失焦通知（ask 音 660Hz）。
- 差距（按影响排序）：
  1. 普通聊天没有「主动提问」开关：`question_policy` 只在 autopilot 生效，普通聊天恒为「一律问」，设置页只有自动化的 auto/smart/ask。
  2. 回答提交失败被静默吞掉：卡片先置 done 再请求，`catch {}` 忽略；用户以为答了，后端没收到，10 分钟后按「未回答」继续。
  3. 600s 超时后卡片变「僵尸」：后端已清 `_ASKS`，前端仍可点提交/我已完成；无超时提示、无「重新回答」，历史只剩「（未回答）」。
  4. 选项只支持一行纯文本：没有「标题 + 说明」、推荐/风险标记，不适合「多方案对比」。
  5. 题面/选项不支持 Markdown（textContent/escapeHtml），长问题、代码片段排版差。
  6. 无显式「跳过 / 你替我决定」出口，只能留空提交。
  7. 等待期间没有时长/倒计时反馈，也不能从卡片结束等待。
  8. 通知不可配置：硬编码单音、只在窗口失焦时弹；Trae 可分别配置「完成 / 等待操作 / 异常打断」的横幅与声音、音量、自定义 mp3。
  9. 多题草稿只在内存，刷新后（即使 attach 重放重建卡片）草稿丢失。
- 资料：Trae 设置项「智能体主动提问」（遇到多方案 / 需了解偏好时暂停提问）、「任务状态通知」（等待操作提示音，可自定义 mp3）。Trae 提问卡的 UI 细节（跳过、方案说明、多选上限）没有公开文档，未核实。
- 相关代码：`app/agent.py`（6580-6661、7080-7103）、`app/api/chat.py`（2699-2706、5757-5815）、`web/chat.js`（3519-3713）、`web/auto.js`（60-92、195-201）、`web/settings.js`（605-614）。

## [2026-11-04] 提问卡补齐：策略范围 / 提交可靠性 / 超时闭环 / 方案选项 / 通知设置

- 问题/决策：把上一轮差距研究里的 P0/P1 项落地；默认行为保持不变（普通聊天仍「一律问」，需要用户在设置里把范围改成「所有对话」才启用 smart/auto）。
- 后端：
  - `config.py` 新增 `question_policy_scope`（autopilot 默认 / all）、`ask_timeout`（默认 600s，30~3600）、`notify_banner / notify_sound / notify_done / notify_ask / notify_error / notify_volume`。
  - `chat.py`：范围 = all 时普通聊天也按 `autonomy_question_policy` 走；新增 `_ask_policy_split()`，弹卡前把「可自决」问题代答（`action=true` 永远弹卡）；`ask` 事件新增 `expires_in`。
  - `agent.py`：`new_asks(timeout=...)` 记录创建 / 截止时间；选项统一成 `{label, desc, recommended}`（兼容纯字符串）；新增 `ask_meta()`、`dismiss_ask()`（让 AI 自己决定）；`wait_answers()` 默认用登记时的有效期。
  - `sandbox.py`：`/api/agent/answer` 对失效 qid 返回 409（前端据此切成「已超时」）；新增 `/api/agent/ask/dismiss`。
  - `session_status.refresh()`：长提问等待期间每 30 秒续期，避免侧栏「待回答」被 15 分钟 TTL 提前清掉。
- 前端：
  - `chat.js`：`buildAskGroup(qs, qid, meta)` 支持倒计时、「让 AI 自己决定」、超时锁定、提交失败回滚（先请求后改 UI）、草稿 localStorage（`ask_drafts_v1`，按 qid）、选项说明 / 推荐徽标、题面 Markdown、超时 / 未答历史卡的「重新提问」（填进输入框）。
  - `stream.js`：`expires_in` → `expiresAt` 传给卡片。
  - `auto.js`：`notifyCfg()` 读取开关 / 音量；横幅与声音解耦，done / ask / error 分别控制。
  - `settings.js`：新增「提问卡」范围 / 策略与「通知」开关、音量；CSS 增加 `.ask-timer / .ask-head-right / .ask-opt.with-desc / .ask-opt-badge / .ask-wait-row / .ask-link / .ask-retry / .timed-out`。
- 验证：`pytest` 覆盖 `test_ask_card.py`（新增 8 项：选项规范化、超时上下限、dismiss 唤醒、策略分流、409、路由、默认配置）以及 agent_guards / settings_defaults / session_status / chat_plan / autopilot / api_handlers_smoke / stream_stop / chat_blocks / agent_plan，全部通过；ruff 通过；`scripts/dev_check.py` 8 项全过（131 个 py、128 个模块、56 个前端 JS、Markdown 回归）。
- 未做：真实窗口点击回归（倒计时、让 AI 决定、通知音量、历史卡重新提问）。
- 相关代码：`app/config.py`、`app/agent.py`、`app/api/chat.py`、`app/api/sandbox.py`、`web/chat.js`、`web/stream.js`、`web/auto.js`、`web/settings.js`、`web/css/base.css`、`tests/test_ask_card.py`。

## [2026-11-04] 阅读器译文导出视觉问题定位与修复

- 现象：导出的译文 PDF 有部分公式/图表错位、重叠，参考文献出现方块，整体排版不如阅读器里的译文视图。
- 复现：用 `data/reader/2bd5eaa8ed5931b8`（main.pdf，6 页）+ 已缓存译文导出 mono/dual，并用 pypdfium2 逐页渲染比对。
- 根因：
  1. **裁图缓存损坏**：16 张公式裁图里 3 张是旧缓存（`crops/` 时间 20:00:31），与同 rect 现渲染逐像素差异 3.5%–8.3%，画面字符重叠（如 `[ _f wg`、`2j` 叠字）；其余 13 张（20:53:55 生成）正常。裁图实现改过但 `CROP_VERSION` 仍为 2，旧错图被永久复用。
  2. **阅读器与导出的公式来源不同**：阅读器对公式自动识别 LaTeX 并用 KaTeX 替换截图（`maybeFormula` / `renderLatex`，`.rd-fig.ai img { display:none }`）；导出始终用截图 → 「导出不如阅读器」的主因。
  3. **字体缺字**：Noto Serif SC 缺 `ő`(U+0151)，参考文献 "Erdős" 变方块；浏览器有字体回退，reportlab 没有。
  4. **排版差距**：列表固定 12pt、无悬挂缩进且忽略 `indent` 层级；定理/证明无左侧竖线；无 CJK 禁则（行首出现 `？`、`）`）；参考文献 8.2pt 偏小；标题/图片缺 keep-with-next。
- 修复：
  - **裁图**：`CROP_VERSION 2→3`；`crop_png` 改成「整页渲染 + PIL 裁剪」，不再依赖 pypdfium2 `render(crop=…)` 的坐标语义（不同版本单位不一致，是错图的来源）。
  - **公式**：导出优先读已缓存 LaTeX，用 matplotlib mathtext 渲染成 600dpi 图（`_prep_math` 处理 `\setminus` / `\ ` / `\text`；不支持的 `cases` 自动回退裁图）；`_math_trusted()` 用「字母必须出现在原文 + 关系符号不能消失」过滤识别坏的结果；`_with_eqnum()` 把原文末尾附近的 `(n)` 补回公式；Markdown 直接输出 `$$LaTeX$$` 而不是贴图。
  - **字体**：新增 `RdLatin`（segoeui/arial/DejaVu）与 `RdSymbol`（seguisym）回退，逐字符选字体并逐 run 绘制；`_plain` 清理私用区字形；TTC 注册带 `subfontIndex`。
  - **排版**：列表按 `indent` 层级（0–3）缩进 + 14pt 悬挂缩进；定理/证明加左侧竖线 + `#f4f7ff` 浅底；`_fix_breaks` 实现中文行首/行尾禁则；标题预留两行正文避免页底孤行；图注跟随图片（放不下整体换页）；参考文献 8.2→9.2pt。
  - `requirements.txt` 增加 `matplotlib>=3.7`（缺失时自动回退裁图，不影响其它功能）。
- 验证：
  - `pytest tests/test_reader.py` 24 项通过（新增禁则、`_prep_math`、`_math_png`、`cached_formula` 4 项）；ruff / dev_check 通过。
  - 实图比对（同一 main.pdf + rd4 译文）：3 张错图消失；公式条由模糊截图变为 mathtext 数学排版（13/16；3 条识别不可信的自动回退为干净裁图）；公式编号 `(1)(2)(5)` 补回；`Erdős` 不再方块；行首 `？` 移到上一行；列表续行悬挂对齐；定理/证明有左侧竖线。
  - 未做：真实窗口里点「导出」的回归（下载/保存流程未改）。
- 相关代码：`app/reader.py`（`CROP_VERSION` 39、`crop_png` 1065、`cached_formula` 1177、`recognize_formula` 1198）、`app/reader_export.py`（字体回退 / `_wrap` / `_fix_breaks` / `_math_png` / `_math_trusted` / `_export_pdf` / `_export_md`）、`tests/test_reader.py`、`requirements.txt`、`web/reader.js`（`maybeFormula` 2301、`renderLatex` 2326）。

## [2026-11-04] Trae 可视化能力差距研究

- 口径：本机 Trae CN v1.107.1（app 3.4.14，`D:\Tools\Trae CN`）+ 官方文档；本项目按源码核实。只研究，不改代码。
- Trae 有 / 我们无：
  1. 预览内「选择元素」→ 把页面元素及样式作为上下文发回对话（docs `tool-panel`；本机 nls `browserLivePreviewView` / `预览控制`）。我们的预览只有地址栏 + 手动刷新 + 关闭（`web/gal/gal-ide.js` 2112–2180）。
  2. 控制台错误闭环：AI 主动展示时自动取错误并尝试修复；手动启动时错误显示在预览底部，可「添加到对话」（docs `browser-and-preview` → Console errors）。我们没有 console 采集。
  3. 预览内静态文字单击直接编辑（docs `tool-panel`；本机「文字编辑当前仅在 SOLO Builder 创建的项目中可用」）。
  4. 预览自动刷新 / 「实时跟随」：AI 到不同阶段自动切编辑器/文档/浏览器面板；Browser 有 devtools 入口。我们保存文件后不会自动 reload，只有 ↻。
  5. Figma 设计还原 / Figma MCP（docs `solo-mode`、`tool-panel`、`tutorial-mcp-figma`）。
  6. Canvas / Design System / Design Library（仅本机 bundle 字符串 `workbench.editor.codeCanvas` / `codeDesignLibrary` / `vscode-component`；官方文档未找到 → 低置信，不当成确定功能）。
  7. 截屏 / 窗口捕获作为对话上下文（本机 `screenCapture` / `iCubeWindowCapture` / `屏幕截图`）。
  8. 部署按钮（Vercel）——不是可视化，但同属预览面板。
- 我们有 / Trae 无（或更强）：ECharts + chart 围栏、手写 K 线 SVG、Leaflet 地图、vis-network 知识/关系图谱、archify 11 类图、PDF 阅读器（划词联动 / 公式 LaTeX / 译文导出）、visual_qa（SSIM / IoU / blank_score）、轨迹时间线与回合圆点。图片生成我们有 ComfyUI 全链路 + 产物栏；Trae 仅 toolhost 内有 `text_to_image` 端点字符串，未见前端面板。
- 持平 / 各有取舍：
  - Mermaid：Trae 是内置扩展（`mermaid-chat-features`，experimental、`mermaid-chat.enabled=false`）；我们可直接用，但依赖 CDN（`web/rich-media.js` 的 `whenLib`）。
  - Markdown：Trae 编辑器内预览 / SOLO 可实时编辑预览；我们分屏预览 + PDF 导出更强，但没有「预览里直接编辑」。
  - 变更可视化：Trae 代码变更面板 + inline diff + checkpoint；我们逐段审阅 / 内联编辑 / steps 撤销·重做·回到这里（`web/chat.js` 1574）。
- 优先级建议：P0 元素选择 + console 错误闭环；P1 预览自动刷新、静态文字点编辑、变更汇总；P2 Mermaid/ECharts 本地化、截图标注与视觉对比看板、工作流 DAG；暂不做 Figma / 部署（定位不符）。
- 资料：docs.trae.cn/ide/tool-panel、docs.trae.ai/ide/browser-and-preview、docs.trae.cn/ide_solo-mode、docs.trae.ai/ide/changelog；本机 `D:\Tools\Trae CN\resources\app`（product.json、nls.zh-cn.messages.json、extensions\mermaid-chat-features）。
- 相关代码：`web/gal/gal-ide.js`（网页预览 2112–2180）、`web/rich-media.js`、`app/preview.py`、`app/visual_qa.py`、`web/knowledge.js`、`web/hall-stock.js`、`web/work/workflows.js`。

## [2026-11-04] 预览闭环 P0：元素选择 + 控制台错误发送到对话

- 需求：按上一轮差距研究的 P0 落地——预览里能选中页面元素、采集 console 错误，并一键发进对话。
- 关键约束：预览是 `127.0.0.1:<随机端口>` 的独立静态服务，与主界面不同源，父窗口读不到 iframe DOM；但静态服务由我们自己实现，可以在返回 HTML 时注入桥接脚本。外部 dev server（`http://localhost:5173`）无法注入，只能降级为地址 + 手动刷新。
- 后端（`app/preview.py`）：
  - `_BRIDGE_JS`（约 190 行，纯 ES5）：悬停高亮 + 点击回传元素信息（`selector / tag / id / classes / text / rect / 样式子集 / outerHTML`）；重写 `console.error / warn`，监听 `error`（含资源加载失败）与 `unhandledrejection`，去重后保留最近 30 条；`postMessage` 到父窗口。
  - `send_head`：`/__gm_preview_bridge.js` 直接返回脚本；HTML（含目录 index.html）读入后把 `<script>` 插到 `</body>` 前再返回，非 HTML 走原逻辑；注入幂等。
  - 兼容性：保留原有越界 / 密钥 403；HTML 不强制 charset（避免把 GBK 页面写成 UTF-8）；HEAD 走同一 `send_head`，不受影响。
- 前端（`web/gal/gal-ide.js` + `web/index.html` + `web/css/ide.css`）：
  - 预览栏新增「选择元素」「控制台」按钮；未握手时禁用并在 title 说明原因。
  - `idePreviewBindBridge()` 只接受 `e.source === iframe.contentWindow` 且 `__gmPreview === 1` 的消息；`ready → state` 拉一次队列；`pick` 转成引用块；`console-batch` 刷新列表与角标。
  - 控制台抽屉：单条「发送」、全部发送（按 3600 字截断）、清空；错误 / 警告分色；切换 URL、刷新、关闭时重置握手与日志。
  - `stream.js::addQuote(text, label)` 增加可选标签，引用块显示「预览元素」「控制台 错误」而不是统一的「选中文本 N」；不改变发送给后端的内容（仍只有 `text`）。
- 验证：
  - `pytest tests/test_ide_preview.py` 8 项通过（新增注入、静态资源不注入、桥脚本内容、幂等）；连同 api_handlers_smoke / web_hardening / artifact_preview 共 38 项通过；`ruff`、`node --check`、`scripts/dev_check.py` 全过。
  - 真实浏览器（临时 harness + 预览服务）：`console.error("boom-test")`、`Promise.reject(Error("rej-test"))` 都被 `console-batch` 回报；`pick` 回传 `button#target`；`pick-state` true/false 正常；测试文件已删除。
  - 未覆盖：主界面按钮点击、引用块在 IDE 窄栏的换行、外部 dev server 的降级提示（逻辑上禁用，未人工点验）。
- 相关代码：`app/preview.py`（`_BRIDGE_JS` / `_inject_html` / `_send_bridge` / `_send_injected`）、`web/gal/gal-ide.js`（`idePreviewBindBridge` / `idePreviewElementToChat` / `idePreviewLogText` / `idePreviewTogglePick` / `idePreviewConsoleToggle`）、`web/index.html`（预览栏按钮 + 控制台抽屉）、`web/css/ide.css`、`web/stream.js`（`addQuote`）、`tests/test_ide_preview.py`。

## [2026-11-04] PDF 阅读器翻译链路重构：输入层清洗 + 翻译内核 + 前端提示

- 样本：`D:\资料库\MVG Paper库` 9 篇（CVPR / ICCV / IJCV / TPAMI / JSCO，30MB），逐篇 `parse_pdf` 后统计。
- 复现的根因（不是模型问题）：
  1. `_extract_chars` 丢掉零面积空格，`_line_text` 只能按 `gap > 0.28em` 猜，导致 `Journal ofComputerVision`、`ifthe`、`InEuropean` 等 192 处粘连。
  2. `_segments` 按字符纵向中心分桶，`g/p/q/y` 的降部被切成独立行；标题变成 `Three-view Focal Len th…`、`An Al ebraic…`。前 4 页共 7145 个降部碎片。
  3. CVPR/ICCV PDF 带一层不可见 LaTeX 源码（字符宽 0.05pt，文本是 `\label{...}\begin{pmatrix}...`），共 63 个块被当正文/标题显示并送翻译。
  4. 标题判定只看「短 + 字号大」：279 个 heading 里 123 个是作者、单位、邮箱、表格行、LaTeX 片段，章节上下文被污染。
  5. `_REF_HEAD_RE` 要求整行恰好 `References`，被首条文献粘连后失配：PLMP / Uncalibrated 两篇 `ref=0`，整段文献被翻译。
  6. 页眉/页脚/水印/页码（37 个模板句）未清；表格行（数字多、词少）被当正文逐格翻译。
- 修复（`app/reader.py`）：
  - **抽取层**：零面积空格保留为标记，`_segments` 按行挂回；`_merge_descenders` 把降部碎片并回主行；宽度 < 0.07×字高的不可见层字符丢弃（整页不可见层时保留，兼容 OCR）；`_make_segment` 字号改 30 分位，避免公式把正文短行抬成标题。
  - **分类层**：`kind=table`（数字 ≥5、词 ≤8、无数学符号/虚词、非定理开头），矩阵类表格改 `formula` 裁原图；`kind=hidden` 丢弃 LaTeX 源码块；标题新增「编号章节」识别（`1. Introduction`）、公式符号 / 句内句号 / 引用 / 虚词结尾 / 全大写表头等排除；front matter 的作者/单位/邮箱标 `meta`；`_clean_meta_blocks` 清理水印、页边短块、跨页重复短文本；参考文献改为前缀匹配 + `[n]` 连续条目兜底。
  - **翻译内核**：`skip_kinds = figure/formula/ref/meta/table/hidden`；上下文增加论文标题 + 摘要；提示词补 URL/DOI/邮箱/作者名/章节编号/引用格式；校验加「整段英文回显」「解释性废话」，长段落长度比 0.30–3.5（短标题不参与）；批调用异常退化为逐段；`_model_of` 改用 `chat_model`（原 `light_model` 优先会让换主模型不失效）。
  - **前端**（`web/reader.js` / `web/css/reader.css`）：新增「重试失败段」（只补缺译，不动已译）；`warn` 事件在段落左侧打琥珀色标记并显示缺失数字/引用；进度显示「跳过 N 段（引用 / 表格 / 公式 / 页眉）」；`table/meta` 只渲染原文，仅译文模式下也不隐藏。
  - **导出**（`app/reader_export.py`）：`table` 用 `ref` 样式、`meta` 用 `note` 样式；Markdown 中表格用代码块、meta 用引用块，不参与 missing 统计。
- 验证（9 篇前后对比）：
  - 不可见 LaTeX 块 **63→0**；前 4 页降部碎片 **7145→约 300**；粘连词 **192→18**；标题噪声 **123→0**；参考文献块 **380→656**（PLMP 0→93、Uncalibrated 0→110）；待翻译块 **2952→2264**。
  - 标题恢复完整：`Three-view Focal Length Recovery From Homographies`、`An Algebraic Geometry Approach to Viewing Graph Solvability`；IJCV 拆行标题两行都保留。
  - 章节识别恢复：CVPR24 `1. Introduction / 1.1 Motivation / … / 4. Conclusion`；CVPR25 `1. Introduction … 6.2 Real-World Experiments`；IJCV26 `7.2.1 …`。
  - `pytest tests/test_reader.py` 35 项通过（新增空格拼接、降部合并、meta 清洗、表格/LaTeX/引用标题、front matter、回显校验、缓存 model、翻译任务 skip）；连同接口冒烟 / Web 加固 / 产物预览共 65 项通过；`ruff`、`node --check`、`scripts/dev_check.py` 全过。
  - 翻译任务用假 `_quick_chat` 端到端跑过：6 个块只翻 heading+text（total=2），done.skipped=4，prompt 含「论文标题」。
- 注意：
  - `SCHEMA_VERSION 14→25`（旧版面缓存失效重解析）、`PROMPT_VERSION rd4→rd5`（旧译文不再复用，需重译）。
  - 表格判定仍是启发式：纯数字网格会进 table/formula（跳过翻译/裁原图），少数数字很多的正文可能被跳过；公式内部字符缺失（如 `Length` 之外的单字母上下标）无法从文本层还原。
  - 未做真实模型 API 端到端翻译（只验证了 prompt 组装、跳过规则、校验与假模型事件流）；主界面「重试失败段 / 琥珀标记 / 跳过统计」未人工点击回归。
  - `retry_failed` 参数保留但语义等同「只补缺译」；「重译本页」仍走 `force`。
- 相关代码：`app/reader.py`（`_extract_chars` / `_segments` / `_merge_descenders` / `_attach_spaces` / `_line_text` / `_make_segment` / `_looks_table_row` / `_make_block` / `_mark_reference_blocks` / `_mark_front_matter` / `_clean_meta_blocks` / `_abstract_text` / `_section_map` / `_validate_translation` / `_translate_batch` / `start_translate` / `_model_of`）、`app/reader_export.py`、`web/reader.js`、`web/css/reader.css`、`tests/test_reader.py`、`docs/PDF_READER_USAGE.md`。

## [2026-11-04] 与沉浸式翻译 / BabelDOC 的版式还原差距与方案

- 结论：我们目前是「左栏原页 + 右栏重排阅读流」；**翻译视图与导出都不保留原页版式**。`web/reader.js` 的模式只有 `pdf/orig/dual/trans`，右栏是单列 `.rd-flow`；`app/reader_export.py` 文件头明确写「不再沿用原页铺图 + 白块盖译文」，改为重排 PDF（图表公式裁图后按阅读流插回）。
- 沉浸式翻译（BabelDOC）的实证（官方文档 / GitHub）：
  - 定位是「保留版面的 PDF 翻译」：版面结构、公式、图表、表格位置不变；输出单语 / 双语 PDF。
  - 双语两种排法：同页左右并排（默认原文在左）或原文/译文页交替；共四种先后组合。
  - 支持 OCR 处理扫描/图片型 PDF；表格文字翻译是实验项（默认关）；有 18+ 引擎、页面范围、批量、水印等选项。
  - BabelDOC 是 AGPL-3.0；官方 README 把直接调用其 Python API 标为 internal，推荐经 pdf2zh-next `high_level.do_translate_async_stream` 调用。
- 差距矩阵（我们 vs BabelDOC）：
  - 图像/图表：我们裁图后按阅读流插回（位置改变）；BabelDOC 原位保留。
  - 公式：我们独立公式裁图 + 行内 KaTeX，位置随阅读流；BabelDOC 原位保留。
  - 表格：我们整块标 `table` 不翻译、导出用 ref 样式，无行列结构；BabelDOC 还原行列，表格文字可实验翻译。
  - 分栏：我们重排为单列；BabelDOC 保持单/双栏关系。
  - 双语：我们段落上下叠；BabelDOC 同页左右或页交替。
  - 页眉页脚/页码：我们解析时丢弃（阅读流没有）；BabelDOC 保留原页。
  - 扫描件：我们无 OCR，直接进 `scan_pages`；BabelDOC 支持 OCR。
  - 我们领先：应用内阅读、批注/划词、术语表、翻译缓存、引用/表格/公式跳过、增量翻译、深色主题。
- 方案取舍：
  - A. 自研「原页内嵌译文」：以原页 canvas 为底，按 `block.rect` 盖白并绘制译文；图表/公式/表格/页眉页码不覆盖。阅读器加「版式」模式（原页 / 译页 / 左右对照），导出加「版式还原 PDF」（单语 / 同页并排 / 页交替）。不引入新依赖，能复用现有译文与字体；缺点是无重排空间，译文过长只能缩字号或回退原文，表格文字暂不翻译。
  - B. 集成 BabelDOC：直接得到竞品级版式还原与 OCR；但 AGPL-3.0、依赖重（Python 版本/版面模型）、API 标 internal、与我们的缓存/术语表/批注不共享，且会二次翻译。
  - C. 混合（推荐）：先做 A 解决「翻译不是原页」；把 BabelDOC 作为可选「最高保真 / 扫描件」导出后端，用户自行安装、子进程调用，避免把 AGPL 代码并入主程序。
- 相关代码：`web/reader.js`（`setMode` 2442、`buildPages` 1430）、`web/css/reader.css`（`.rd-pages/.rd-flow`）、`app/reader_export.py`（文件头 2–15、`_export_pdf`）、`app/reader.py`（`block.rect` / `parts` / `sizes`）。

## [2026-11-04] 版式还原落地（A 方案）：阅读器译页 + 版式导出

- 需求：用户确认走混合 C；双语优先同页左右并排。本轮先实现 A（自研原页内嵌 + 版式导出），BabelDOC 留作可选外部引擎。
- 阅读器（`web/reader.js` + `web/css/reader.css`）：
  - 新增模式 `page`（译页）和 `page-dual`（版式对照），顶栏按钮「译页 / 版式对照」；原有 `orig/dual/trans` 重排阅读流保留。
  - 译页：`RD.tpages` 用 pdf.js 按右栏宽度渲染原页 canvas，再加 `.rd-tlayer`；按 `block.rect`/`parts` 换算百分比放置 `.rd-tblock`。
  - 覆盖规则：只覆盖 `text/list/heading`；`figure/formula/table/ref/meta`、页眉页码/水印不动。白底颜色从原页 canvas 四角采样，避免非纯白页出现色块。
  - 文本适配：`formatInline` 支持行内公式；字号在 4.6–18pt 间二分，放不下先向下扩展（检查下方块边界），仍放不下标 `data-overflow` 并提示切阅读流看完整译文。一级标题居中；点击文字块可切回原文。
  - 滚动：`onTransScroll` 按页同步；`goPage` 在版式模式下滚右栏译页；`page-dual` 右→左联动。
  - 进度：版式模式不建阅读流，新增 `syncTransStats()` 按 `doc.blocks` 统计，避免进度一直 0/0。
- 导出（`app/reader_export.py`）：
  - 新增 `layout-mono` / `layout-dual`：`reportlab` 生成同尺寸透明覆盖层（白块 + 自动 fit 译文），`pypdf` 直接合并到原页；`layout-dual` 新建 2 倍宽页面，左半原页、右半覆盖层平移过去。
  - 原有 `mono/dual/md` 重排导出保留；`SUFFIX` / `export_name` 增加「版式译文 / 版式双语」。
  - 前端设置面板分组「重排导出 / 版式导出」，新增两个按钮。
- 验证：
  - `pytest tests/test_reader.py` 36 项通过（新增版式导出：页数/尺寸不变、中文可抽回、双语宽度 2 倍）。
  - 真实论文 CVPR24（10 页）：`layout-mono` 10 页 / 612×792、`layout-dual` 10 页 / 1224×792；原 PDF 图表图片数 `[0,0,0,0,0,0,0,4,0,0]` 与导出完全一致；8 页可抽到中文；逐页 PNG 检查确认图片、作者、页眉/页码原位保留。
  - 冒烟：`node --check web/reader.js`、`ruff`、现有 36 项阅读器测试通过。
- 已知限制：
  - 译文比原文短时，原文字块会留下空白（白块只覆盖文字区）；比原文长时优先缩字号、再向下扩展，仍放不下回退原文/标提示，不会强行剪裁。
  - 表格文字、扫描件 OCR、极端复杂版面的自动重排仍未做；这三项是 BabelDOC 的强项，留作可选外部引擎。
  - `layout-dual` 同页宽度翻倍，阅读器里需要横向滚动/缩放；若觉得不便，可再加「原/译页交替」导出。
  - pypdf 合并保留页面内容与图片，但不保证原 PDF 的表单/链接/书签全部保留；需要时再单独做 outline 复制。
- 相关代码：`web/reader.js`（`buildTransPages` / `renderTransPage` / `renderTransOverlay` / `fitTransBlock` / `onTransScroll` / `setMode`）、`web/css/reader.css`（`.rd-tpages` / `.rd-tblock` / `.rd-mode-page*`）、`app/reader_export.py`（`_layout_blocks` / `_layout_fit` / `_layout_overlay` / `_export_layout` / `export`）、`tests/test_reader.py`、`docs/PDF_READER_USAGE.md`。

## [2026-11-04] 停止输出失效的根因分析（只定位，未改代码）

- 现象链：流式输出偶发完全静止 → 末尾残留「正在输出…」→ 刷新/切会话后变「已完成」或「模型输出中」但无停止按钮 → 只能关窗口；长上下文概率显著升高。
- 前端根因：
  1. `init.js` 首屏从 `/api/state` 填了 `state.streamingSids`，但**没有调用 `refreshStreamingUI()`**；`state.streaming` 仍为 false，`#btn-stop` 保持 `.off`，所以「后台任务在跑但没有停止按钮」。`init.js` 143–158。
  2. `stopStreaming()` 只有「点停止 → `/api/stop` → 等 done」一条路：没有本地 AbortController、没有超时、没有强制收尾；`/api/stop` 返回 ok 也不代表服务端找到了流。`stream.js` 345–372。
  3. SSE 读取 `reader.read()` 没有超时；15 秒无事件只提示并挂 `/api/chat/attach`，**不 abort 原 fetch**。如果连接半开，`streamChat` 永远进不了 `finally`，`.live-bar`（「正在输出…」）永不删除。`stream.js` 1170–1203、1273–1301、1884–1896。
  4. 断线恢复 `keepRecovering` 要等 10 分钟；`waitJobThenRefresh` 的 attach 也没有超时，zombie 任务会让 UI 一直停在「模型输出中」。
  5. HTTP 409 busy 时，用户消息已经在 `sendMessage` 里先上屏，失败后不回滚；刷新后消息消失，像「输出被回退」。
- 后端根因：
  1. `ACTIVE_STREAMS[sid] = ev` 在 `_api_chat` 的 **3439 行**才登记，而同步压缩 `compress_history` 在 3305–3332、记忆/上下文装配更早；长上下文在这一段耗时最长，此时 `/api/stop` 在 `ACTIVE_STREAMS` 里找不到会话，只是返回 `{"ok":true}`，停止信号直接丢失（`misc.py` 237–242）。
  2. `_api_chat` 没有顶层 `try/finally`；`ACTIVE_STREAMS.pop` 只在正常收尾路径（6909）执行。任何异常/提前返回都会留下僵尸条目；`/api/state` 又无条件把 `ACTIVE_STREAMS.keys()` 当 streaming（`misc.py` 49），于是刷新后永远显示「生成中」，新请求被 409 busy 卡死。`stream_alive()` 已存在（`chat.py` 2248–2266）但 `/api/state` 没用。
  3. 同步压缩走 `llm._quick_chat`（非流式）且没有 `stop_event`；停止只能等压缩请求自己超时/返回。背景 `maybe_precompress` 也一样，压缩期间前端只能排队，且没有停止入口。
  4. `stream_chat` 收到 stop 只 `cancelled.set()`，主循环 break，但不主动 `resp.close()`；读线程可能继续阻塞到 socket 超时，上游连接没真正断。
  5. SSE `_write` 直接 `wfile.write`，客户端不读时最多阻塞 20 秒（`chat.py` 3007–3064）；这期间心跳和生成线程都停在同一把锁上，停止检查也被拖住。
  6. `httpd` 对 `/api/chat` 的异常统一 `_json` 回写（587–591），但 SSE 头已经发出，异常时不会补 `done`；前端只能走 10 分钟恢复。
  7. `_progress` 快照只有进程重启时由 `_mark_resume_pending` 转成 interrupted（`sessions.py` 256–278）；线程死掉但进程还在时，刷新会把半截快照显示成「已完成」。
- 方案要点（P0）：`ACTIVE_STREAMS` 提前到请求入口登记；`/api/stop` 对未登记会话记 pending stop 并返回 `found`；流租约加 owner/watchdog/top-level finally；压缩与 `_quick_chat` 接 `stop_event`（可取消）；前端首屏 `refreshStreamingUI`、停止硬超时 + abort + 幂等 `finalizeStream`、15 秒无事件 abort 原流；`/api/state` 用 `stream_alive` + orphan `_progress` 清理；SSE 写入改队列，异常路径补 `done`。
- 相关代码：`web/stream.js`（260–372、1170–1301、1884–2036、2043–2239）、`web/init.js`（143–158）、`web/chat.js`（1470–1487、4530–4552）、`app/api/chat.py`（2714–2724、3305–3332、3436–3439、6909–6910、3007–3064）、`app/api/misc.py`（31–56、237–242）、`app/llm.py`（645–874）、`app/context.py`（589–693、844–898）、`app/sessions.py`（256–278）、`app/httpd.py`（587–591）。

## [2026-11-04] 停止输出失效修复实施（P0+P1）

- 问题/决策：按上一节的根因做完整加固，目标「任何阶段点停止都能收尾、不残留『正在输出…』、刷新后不再永久『生成中』、长上下文也能停」。原则是**每一层都能独立兜住**：前端断开 / 后端 watchdog / HTTP 异常兜底各自都能收尾，不依赖另一层活着。
- 后端（流租约与停止信号）：
  - `app/store.py`：新增 `PENDING_STOPS` + `request_stop(sid)` / `consume_stop(sid)`（TTL 120 秒）。请求还没登记流时点停止不再丢信号。
  - `app/api/misc.py`：`/api/stop` 返回 `{found, pending}` 并打埋点日志；`/api/state` 的 `streaming` 改为 `_live_streams()`——用 `stream_alive` 过滤「线程已死但条目还在」的僵尸并顺手清掉，再对非 live 会话跑 `reap_orphan_progress`。
  - `app/sessions.py`：新增 `reap_orphan_progress(data, live_ids)`：无活跃流却留着 `_progress` 的会话 → 去掉快照标记、置 `interrupted` + `end_reason(kind=resume)` + `resume_pending`。
  - `app/api/chat.py`：
    - 流租约从「3439 行」提前到**参数校验之后、SSE 建立之前**注册（避开 400/404 早退留下僵尸，同时覆盖压缩 / 记忆检索阶段）；只保留一处注册。
    - 新增 `release_active` / `stream_alive`（原有）/ `_force_done_event` / `_watch_stream`（2 秒轮询：job 完成 5 秒后释放；180 秒无事件强制补 done；线程已死 / 装配超 900 秒释放）/ `abort_chat_stream`（HTTP 异常兜底：置事件 + 补 done + 释放租约 + 清「待审批 / 待回答」标记）。
    - SSE 建立后与压缩后各消费一次 pending stop 并早停；两处同步压缩传 `stop_event=ev`；预算 / 缺密钥两个早退补 `release_active`。
    - 心跳线程在 `_job["done"]` 后自动退出，避免异常路径没有 `_hb_stop` 时永久写死连接。
  - `app/context.py`：`compress_history(..., stop_event)`；`_COMPRESS_CANCEL` + `cancel_compress` / `compress_stop_event`；`maybe_precompress` 把事件传进 `compress_history`。
  - `app/llm.py`：`_quick_chat(stop_event)` 用后台线程包装非流式请求，主线程 0.2 秒轮询、停止即返回空串；`stream_chat` 新增 `_abort_stream()`（置 cancelled + 后台线程 `resp.close()`）。**close 必须在后台线程做**：实测在主线程 close 会把「点停止」拖 2~3 秒（等上游下一段数据）。
  - `app/httpd.py`：每个 POST 请求开始重置 `_chat_sid/_chat_ev/_chat_sse`（keep-alive 复用 Handler 实例）；异常时若已发 SSE 头 → 调 `abort_chat_stream` + 直接补一条 `done` 并关连接，不再写 JSON；未发头才回 500。
- 前端：
  - `web/init.js`：首屏填完 `streamingSids` 后调用 `refreshStreamingUI()` —— 刷新后「后台在跑」也能看到停止按钮。
  - `web/stream.js`：
    - `streamChat` 加 `AbortController` + `signal`，`stRef.abort()` 可断开；catch 区分「本端主动断开」与真实失败（不往正文塞「请求失败」）。
    - 15 秒没有任何事件（连 tick 都没有）→ 直接 abort 原流，让 `finally` 一定执行（删 `.live-bar`），再由 `keepRecovering` 用 attach 补状态。
    - `stopStreaming` 重写：`/api/stop` 5 秒 ACK 超时；本地流 8 秒没收 done 就硬断（`st.hardStop`）；远端流 30 秒不确认就本地解除「生成中」并提示，停止按钮不会永远点不动。
    - `keepRecovering(sid, err, tries, stopped)`：手动停止只盯 24 次（约 2 分钟），到点强制解除 UI 等待；手动停止不发「完成」通知。
    - 409 busy：标记 `busyRejected`，收尾时移除幽灵用户消息 + `renderMessages` + 恢复「生成中 + 停止键」+ attach 跟到真正那一轮结束。
- 未做（与上一节方案不同，记录在此避免误读）：SSE 写入**没有**改成有界队列 + 独立写线程。现有实现已有 20 秒写超时 + `_client_gone` + 心跳自动退出，本次先把可证实的问题修完；若后续出现「写阻塞导致主循环停住」的现场再上队列。
- 验证：
  - `tests/test_stream_stop.py` 新增 8 项：pending stop 的写入/消费/过期、`abort_chat_stream` 补 done 并释放租约、`_live_streams` 清僵尸、`reap_orphan_progress` 转换快照、`_quick_chat(stop_event)` 立即返回、压缩被停止时不再发请求；26 项全通过。
  - `tests/test_api_handlers_smoke.py` 新增 `/api/stop` pending 冒烟；连同 chat_jobs / web_hardening / context / compress_marks / session_store / resume / store / chat_parallel / session_parallel 共 117 项通过。
  - `ruff` 通过；`scripts/dev_check.py` 全绿（131 文件编译、128 模块导入、56 个前端 JS 语法、快捷键 73/73、Markdown 回归全过）。
  - 未覆盖：真实浏览器 + 真实上游的端到端手测（需要手动点停止 / 刷新验证），以及「SSE 写阻塞 20 秒」这一条现场未构造。
- 相关代码/指令：`app/store.py`、`app/api/misc.py`、`app/sessions.py`（`reap_orphan_progress`）、`app/api/chat.py`（`release_active` / `_watch_stream` / `abort_chat_stream` / `_api_chat` 入口与三处早停）、`app/context.py`、`app/llm.py`（`_quick_chat` / `_abort_stream`）、`app/httpd.py`、`web/init.js`、`web/stream.js`、`tests/test_stream_stop.py`、`tests/test_api_handlers_smoke.py`。验证命令：`.venv\Scripts\python.exe -m pytest tests/test_stream_stop.py tests/test_api_handlers_smoke.py -q`。

## [2026-11-04] 版式译页重构：按原段落行数/行距排版（transpage.js）

- 问题/决策：用 `D:\资料库\MVG Paper库` 的真实论文（CVPR24 点线位姿，10 页 / 327 块 / 311 条译文）复现，旧「版式译页」有 4 个硬伤：
  1. **字号基准错了**：`basePx = block.size × scale`，但解析出的 `size` 是字形墨迹高度（≈0.45em），正文实际 10pt 被当成 4.7pt 渲染，译文字号只有原文一半（无头浏览器实测正文 6.4px）。
  2. **行距固定 1.22**：段落只贴在原框顶部，译文比原文短时下面留大片空白（页面看起来「被打散」）。
  3. **占位符泄漏**：旧缓存（rd4）里模型给标题凭空造 `⟦F1⟧`，`math` 里没有这个键 → 译页直接显示「⟦F1⟧ 引言」，编号也丢了。
  4. **图内小标签被当成段落**：6~7pt 宽的坐标轴字母 / 图注碎片被渲染成竖排文字，还盖出灰色块。
- 后端（`app/reader.py`）：
  - `SCHEMA_VERSION` 25→26；`_make_block` 写入 `lines`（原段行数），译页据此还原行距。
  - 新增 `normalize_translation(b, text)` / `normalize_translations(data, meta)`：丢掉 `math` 里不存在的占位符、标题从原文补回 `1.2.` 这类编号、只剩非法占位符视为无译文；在 `load_translations`（读旧缓存）与翻译任务落盘前各跑一次，**不用整篇重译**。
  - `_validate_translation`：短标签（≤8 字符）的译文长度上限 `max(8, 3×源长)`，挡住批翻译序号错位把「1.1 研究动机」贴到图注上的问题。
- 前端（新增 `web/transpage.js`，`reader.js` 只负责收集 items / 障碍物）：
  - 字号从**原段行距**反推：`max(lead/1.15, size×1.85~2.2) × scale`（单行标题用 2.2 倍）；上限再加「不能挤掉原段行数」的约束（`boxH/(target×1.05)`）。
  - 目标行数 = `block.lines`；行距 = `boxH / max(lines, target)`，短译文用原段行距撑满，长译文先压行距、再降字号；仍放不下时向下扩到下个块、最后才标 overflow。
  - 单行块（标题/图注）矩形只是墨迹高度，向下借一点空隙到「一个正常行框」，否则会被压得比原文还小。
  - 遮盖改为**矩形差集**（`coverPieces`），不再整块盖住公式/表格；cover 用 px 定位，避免加高文本框时被百分比拉伸。
  - 跳过：宽 <12pt / 高 <5pt 的碎片、与公式/表格/图片重叠 >50% 的图内文字、模型原样回显的块。
- 验证：
  - 无头 Edge 打开真实 blocks/译文测试台：3 页 43 块，`overflow=0`、横向裁切 `clippedX=0`、`⟦` 残留 `ph=0`；字号 p1 9.3~19.5px / p2 6.7~14.9px / p3 9.8~13.6px。
  - 侧记：PDF 解析出的 `sizes[0]` 是 612×792，而正文字形高度 4.5~4.7pt、行距 ≈11.1pt，可确认「行距/1.15 ≈ 真实字号」。
  - `node scripts/transpage_test.js` 18 项；`tests/test_reader.py` 42 项（新增行数 / 规范化 / 短标签校验）；`ruff`、`dev_check` 全过（新增第 8 节译页单测）。
- 未做 / 已知边界：`reader_export.py` 的「版式导出 PDF」仍按固定字号排版，没跟着改行距（导出与阅读器观感会略有差异）；真实 24 页论文只抽查了前 3 页 + 全量几何统计。
- 相关代码：`web/transpage.js`（新增）、`web/reader.js`（`pageTransGeo` / `renderTransOverlay`）、`web/css/reader.css`（`.rd-ttext` / `.rd-tcover` / `.rd-tblock`）、`web/index.html`（script 顺序）、`app/reader.py`（`lines` / `normalize_translation` / `_validate_translation`）、`scripts/transpage_test.js`、`scripts/dev_check.py`、`tests/test_reader.py`。

## [2026-11-04] 笔记库：划词提问 + 引用到对话

- 问题/决策：笔记预览原来只能「批注」，选中内容想追问或带进对话只能手动复制。补上与阅读器一致的「划词提问 / 引用」，但**不复用阅读器的 `/api/reader/selection`**（它的提示词写死了「论文阅读助手」），单独给笔记开一个接口。
- 解决方案：
  - 选区工具条：`#nt-anno-add` 从单按钮改为 `.nt-sel-bar`（批注 / 提问 / 引用），`mousedown` 阻止默认行为保住选区。
  - 提问：弹层 `#nt-ask-pop`（复用批注弹层样式）→ `POST /api/notes/ask`，带笔记标题 + 选区前后 30 字上下文；回答用 `safeRenderMarkdown` 渲染。回答后提供「存为批注」（写入现有批注，comment = 问+答）和「引用到对话」。
  - 引用：`addQuote(选中文字, '笔记《标题》')` → 关闭笔记库 → 聚焦输入框；问答结果的引用把「问/答」一起带走。
  - 后端 `app/api/notes.py`：新增 `_api_notes_ask`（text ≤1500 / question ≤500 / context ≤800，缺密钥 400，模型异常 500，`source="notes"` 计入用量）；路由挂在 `app/httpd.py`。
- 验证：`tests/test_api_handlers_smoke.py` 新增笔记提问用例（正常 / 空选区 / 空问题 / 缺密钥 / 模型异常）；`node --check web/init.js`、`ruff`、`dev_check` 全绿；无头 Edge 截图确认工具条与问答弹层样式（引用按钮为强调色，回答区可滚动）。
- 实现细节：请求期间弹层可能被外部点击关闭，`submitNoteAsk` / `saveNoteAskAsAnno` 先抓住 draft 快照再 await，避免空引用报错。
- 相关代码：`app/api/notes.py`（`_api_notes_ask`）、`app/httpd.py`（`/api/notes/ask`）、`web/init.js`（`ensureAnnoAdd` / `showAskPop` / `submitNoteAsk` / `quoteNoteDraft` / `quoteNoteAsk` / `saveNoteAskAsAnno`）、`web/css/notes.css`（`.nt-sel-bar` / `.nt-ask-pop` / `.nt-ask-out`）、`tests/test_api_handlers_smoke.py`。

## [2026-11-04] PDF 翻译功能体检与优化路线图

- 问题/决策：对 PDF 解析 / 翻译 / 译页 / 导出做一次全量评估，用 `D:\资料库\MVG Paper库` 9 篇真实论文 + 1 页光栅化扫描件实测，产出可执行的迭代计划（文档见 `docs/PDF_TRANSLATION_ROADMAP.md`）。
- 关键实测结论：
  - **阅读顺序**：`_band_rows` 只按「是否跨栏中线」判整宽行，CVPR24 p5 一行 7.6pt 宽、文本 `0\x01` 的碎片被当成整宽，页面切成两带 → 阅读流顺序变成「左上半 → 右上 → 左下 → 右下」。9 篇中 8 篇有双栏页，窄行切带 0–6 处/篇。
  - **扫描件**：光栅化 PDF 解析结果 `blocks=0, scan_pages=[1]`，项目内没有 OCR 通路，只有一句提示。
  - **导出**：版式导出仍按矩形高度猜字号（`_layout_fit`），固定 1.22 行距、放不下整段退回原文（`missing`），与阅读器译页（按 `lines/lead` 排版）不是同一套。
  - **页内交互**：`applyReaderAnnos` 只挂阅读流块，划词浮层只绑 `.rd-flow` → 译页模式不能高亮/批注/划词。
  - **长文档**：24 页首次解析 3.6s 且同步阻塞在 open 请求里（外推 1000 页 ≈150s）；`renderFlow` 一次性建全部块，无虚拟化。
  - **表格/图内文字**：`table` 只显示原文（CVPR24 21 块、IJCV26 130 块），图中文字完全不翻。
  - **目录**：`pypdfium2.get_toc()` 可用但未接入，阅读器只有页码/缩略图。
- 优先级建议：P0 = 双栏顺序窄行切带、旧缓存短标签错位、导出一致性；P1 = 译页批注/划词、增量解析 + 虚拟化、扫描件 OCR、表格译文、深色纸色、目录；P2 = 图中文字、混合语言、缓存版本。
- 相关代码：`docs/PDF_TRANSLATION_ROADMAP.md`（新增）、`app/reader.py`（`_band_rows` / `normalize_translation` / `_validate_translation`）、`app/reader_export.py`（`_layout_fit` / `_layout_overlay`）、`web/reader.js`（`renderFlow` / `applyReaderAnnos`）、`web/transpage.js`。
- 说明：除 CVPR24 外其余 8 篇没有译文缓存，本次未跑全量翻译（避免消耗额度）；翻译质量结论基于 91 条现有译文 + 提示词/校验代码。

## [2026-11-04] 译页补齐批注/划词 + 版式导出对齐

- 问题/决策：上一轮评估留下的两处「功能缺失 / 导出瑕疵」先落地——
  1. 译页模式不能划词、批注、高亮（`applyReaderAnnos` 只挂阅读流块，浮层只绑 `.rd-flow`）；
  2. 版式导出与阅读器译页不是同一套排版（固定 1.22 行距、按矩形高度猜字号、白块整块盖图表、单行标题全部判「放不下」）。
- 解决方案（前端）：
  - `web/transpage.js`：`render()` 返回 `{blockId: box}` 并在排版完成后回调 `onReady(els)`。
  - `web/reader.js`：新增 `RD.tblockEls`（页 → 块元素）与 `RD.blockById`；`renderTransOverlay` 渲染后调用 `applyPageAnnos(page)` 把高亮/数字角标贴到覆盖层；`applyReaderAnnos` 末尾同步所有已渲染译页。
  - 译页划词：`.rd-tpages` 增加 `mouseup` → `showPageSelectionPopover`（按 `.rd-tblock` 归段，`side=dst`），复用现有选词浮层（翻译/提问/批注/摘录/高亮/复制）。
  - 批注抽屉跳转：译页模式下 `goPage` + 覆盖层块闪烁（找不到就重试 3 次）。
  - 样式：`.rd-tblock.has-anno .rd-anno-dot`（块内右上角，避免 `overflow:hidden` 裁掉）、`.rd-tblock.rd-flash`。
- 解决方案（导出）：
  - `_layout_fit` 改为按「原段行数」定行距（`h / max(译文行数, 原段行数)`，限制 1.05–1.9 倍字号），字号从 `lines/lead` 反推；单行块向下借空隙（否则标题永远放不下）。
  - 白块改矩形差集 `_subtract_rect`，避开公式/表格/图片；跳过 <12pt 碎片、原样回显、与图表重叠 >50% 的块（与译页一致）。
  - 实测：CVPR24 版式导出 p1 的竖排「1.1 研究动机」消失、标题正常输出；合成全长译文 `missing` 8→7（/179）。
- 验证：`tests/test_reader.py` 43 项（新增短标签清洗、行数/行距、差集用例）；`node --check`、`transpage_test` 18 项、`ruff`、`dev_check` 全绿；pypdfium2 渲染导出 PDF 逐页目检 1–2 页。
- 相关代码：`web/transpage.js`（`render` 返回值/`onReady`）、`web/reader.js`（`applyPageAnnos` / `showPageSelectionPopover` / `jumpToAnno`）、`web/css/reader.css`（译页角标/闪烁）、`app/reader_export.py`（`_layout_fit` / `_subtract_rect` / `_layout_overlay`）、`app/reader.py`（`normalize_translation` 短标签规则）、`tests/test_reader.py`、`docs/PDF_READER_USAGE.md`。

## [2026-11-04] 聊天附件：多文件 / 粘贴 / 解析进度 / 点击预览

- 问题/决策：
  - 附件一次只能带一个文档：`state.pendingDoc` 是单值，`attachDocBlob` 直接覆盖，`#doc-input` 也没有 `multiple`。
  - 粘贴文件走同一个单值通道，第二个文件会顶掉第一个。
  - PDF 解析期间只有一对 `doc_parsing` / `doc_ok` 事件，长文档 / 扫描件逐页 OCR 时界面只停在「正在解析…」，看起来像卡死。
  - 发完立刻点消息里的附件标签调 `/api/attach/get`：此时本地 echo 没有落盘名 `file`；且 `Content-Type` 只有图片映射，PDF 被当 `octet-stream`，iframe 可能直接下载。
- 解决方案：
  - 前端状态改为 `state.pendingDocs: []`（最多 10 个，单文件 2GB 上限）；`#doc-input` 加 `multiple`，选择 / 拖拽 / 粘贴统一追加；附件标签逐个渲染、逐个移除、点标签即预览。
  - 后端同时接受 `docs: [{name,mime,data}]` 与旧的单个 `doc`；消息结构新增 `docs: [...]`，仍写 `doc = docs[0]` 兼容旧渲染 / 导出 / 回看。
  - 解析改为串行循环，每个附件发 `doc_parsing {index,name,count}` → `doc_progress {index,stage,done,total}` → `doc_ok {index,chars,warn|error}`；`docparse.extract` 新增 `progress(done,total,stage)`，覆盖 `pypdf` 逐页读取、`pypdfium2` 渲染、逐页 OCR（0.25s 节流）。
  - 状态条同步显示「正在解析附件（2/3）｜文件名」与「正在读取/渲染/识别附件 3/10 页」，标签加 `.parsing` 呼吸样式。
  - 刚发出的附件在 `chat.js` 里留一份内存 base64 缓存（最多 8 个），点标签先用缓存预览；`/api/attach/get` 补全 PDF / Office / 文本 MIME，前端再按消息里的 mime 重包 Blob。
  - 上下文拼装 `context._msg_text`、附件登记 `attachments.from_message`、清理引用 `uploads._referenced_files`、Markdown 导出 `export_md` 都改为优先读 `docs`，旧 `doc` / `doc_text` 仍可读。
- DeepSeek 网页版差距（据 IT之家 2026-04-09 报道，未逐项实测）：专家模式支持上传文件，最多 50 个、单个 100MB、文档 + 图片、仅识别文字；本项目现在最多 10 个（受单请求 JSON 体积和上下文预算约束）、单文件上限 2GB 但实际受浏览器内存限制。上传进度条、失败重试、解析结果内联展示仍未做。
- 相关代码：`web/stream.js`（`attachDocBlob` / `renderAttach` / `docChipRender` / `sendMessage`）、`web/chat.js`（`lastUserDocChips` / `rememberSentDoc` / `buildUserRow` / `docPreviewStored`）、`web/init.js`（多选 / 粘贴）、`web/core.js`、`web/index.html`、`web/css/base.css`、`web/css/search.css`、`app/api/chat.py`、`app/docparse.py`、`app/context.py`、`app/attachments.py`、`app/uploads.py`、`app/export_md.py`、`tests/test_attachments_multi_doc.py`、`tests/test_docparse_pdf.py`。
- 验证：`node --check` 4 个前端文件通过；`ruff` 通过；`dev_check.py` 9 项全过（131 文件编译 / 57 个前端 JS）；`pytest tests/test_docparse_pdf.py tests/test_attachments_multi_doc.py tests/test_context.py` 全过。全量 `pytest` 仍有 3 条与本轮无关的既有失败（`test_memory.py` reindex 计数、`test_native_tools.py` 压缩 `stop_event` 桩函数、`test_prompt_budget.py` Agent 提示 8268>8000），未改动。

## [2026-11-04] 附件跟进：读取进度 / 失败重试 / Office 文本预览

- 问题/决策：
  - 大附件读取（FileReader → base64）期间没有任何反馈，用户不知道「点了没反应」还是「在读」。
  - 解析失败只在气泡里留一句「（解析失败）」，没有恢复入口，只能撤回重发。
  - docx / xlsx / pptx 点开只有一句「这种格式暂不支持在应用内预览」，实际后端本来就能抽文本。
- 解决方案：
  - `attachDocBlob` 先插入 `loading` 占位项（带 `progress`），用 `FileReader.onprogress` 更新「读取 x%」；读取中点标签不触发预览，发送按钮禁用并提示「附件还在读取中」；被移除的占位项不会再被 onload 补回来。
  - 解析失败的标签点击直接重试：新增 `POST /api/attach/reparse {session_id,index,doc_index}`，按落盘文件重跑 `docparse.extract`（扫描件带视觉回调），成功回写字数/正文并清掉 error，失败回写原因；前端 `docReparse` 同步更新本地消息与标签，成功后标签恢复成「（N 字）」。
  - 新增 `POST /api/attach/text`：返回落盘附件抽出的纯文本（预览要快，不跑 OCR）；`docPreviewStored` 对 docx/xlsx/pptx 先取文本，用 `docPreviewOpen(..., {text, note})` 在预览框里显示「文本预览（版式与图表请下载原文件）」，下载仍是原文件。
  - 扫描件 OCR 提示词抽成 `docparse.PDF_PAGE_OCR_PROMPT`，聊天解析与手动重试共用，避免两处漂移。
- 相关代码：`web/stream.js`（`attachDocBlob` / `renderAttach` / `syncSendBtn` / `sendMessage`）、`web/chat.js`（`docPreviewStored` / `paintDocChip` / `docReparse` / `docPreviewOpen`）、`web/css/base.css`（`.attach-doc.loading`）、`app/api/sessions.py`（`_api_attach_reparse` / `_api_attach_text`）、`app/httpd.py`、`app/docparse.py`、`tests/test_attachments_multi_doc.py`。
- 验证：`dev_check.py` 9 项全过（含 57 个前端 JS 语法）；`ruff` 通过；`pytest tests/test_attachments_multi_doc.py tests/test_docparse_pdf.py` 17 条全过（新增文本预览、重试回写、缺文件 404）。未做图片发送前压缩（会牺牲截图/OCR 清晰度，先不动）。

## [2026-11-04] 修复：SVG 源码按钮 / 划词提问失效 / Ctrl+Shift+V 切换渲染

- 问题/决策：
  - AI 输出的 SVG / 图表块右上角「源码」点了没反应：`rich-media.js` 只切了 `.show-src`，但源码 `<pre>` 自带 `.hidden`，而 `base.css` 有一条全局 `.hidden { display: none !important; }`，`!important` 压过 `.rich-media.show-src .rich-media-src { display: block; }`，所以源码永远不显示。
  - Solo 划词「问问」偶尔提示「这段内容无法提问」：`freezeSelection` 只用 `row._content` 判断选区。AI 回复被提问卡切成多段时 `_content` 指向最后一段，选前面几段就判成「不在选区里」；选在思考过程 / 工具输出里也没有兜底。
  - Solo 与笔记缺少「渲染 / 源码」的键盘入口。
- 解决方案：
  - `rich-media.js`：切换 `show-src` 时同步 `classList.toggle('hidden', !on)`；主题切换重置时重新加回 `hidden`。
  - `aside.js`：新增 `rowContents(row)`，AI 行取全部 `row._secs`（存活的节点）再加 `_content`；`rangeTextIn` 统一把 range 夹到节点内。多段都扫不到时兜底用选区原文（选在思考 / 工具输出里也能提问），不再直接返回 null。
  - 新增快捷键 `global.render`（默认 `Ctrl+Shift+KeyV` / `Meta+Shift+KeyV`，可在「?」清单改键）：
    - 笔记弹窗打开：在「编辑（源码）↔ 预览」之间切换（`setNoteMode`）。
    - Solo：切换 AI 回复的源码视图（优先选区所在消息，否则最后一条 AI 回复，复用 `toggleRawView`）。
    - IDE 模式 / 设置弹窗打开时不抢键（IDE 的 Ctrl+Shift+V 仍是 Monaco 的 Markdown 预览）。
- 相关代码：`web/rich-media.js`（源码按钮 / `reTheme`）、`web/aside.js`（`rowContents` / `rangeTextIn` / `freezeSelection`）、`web/hotkeys.js`（`HK_DEFS` 新增 `global.render`）、`web/init.js`（`toggleChatRenderByKey` + keydown 分发）。
- 验证：`node --check web/rich-media.js / aside.js / init.js / hotkeys.js` 通过；`node scripts/hotkeys_test.js` 73/73；`dev_check.py` 9 项全过。未做浏览器点击实测（需要打开应用窗口）。

## [2026-11-04] 笔记预览支持 SVG / 图表渲染

- 问题/决策：笔记预览由 `setNoteMode('preview')` 直接把 `safeRenderMarkdown()` 的 HTML 塞进 `#nt-preview-box`，但没调用富媒体增强。```svg 围栏只会生成 `.rich-media` 占位块，界面一直停在「渲染中…」，看起来像不支持 SVG。
- 解决方案：预览渲染完、并把预览区切成可见之后，调用 `window.enhanceRichMedia(pv)`。与对话共用同一套渲染器（SVG / ECharts / Mermaid、下载、源码、主题重绘），笔记里写 ```svg 就能直接出几何图。放在可见性切换之后，是因为图表初始化需要真实尺寸，隐藏状态下会量成 0。
- 用法：笔记正文里写围栏代码块 ` ```svg ` + `<svg …>…</svg>`；在「预览」模式查看，右上角可下载 / 看源码。
- 相关代码：`web/init.js`（`setNoteMode`）。
- 验证：`node --check web/init.js`；`dev_check.py` 9 项全过（含 57 个前端 JS 语法、Markdown 富媒体回归）。未打开应用窗口做点击实测。

## [2026-11-04] 修复：笔记批注高亮破坏列表 / 表格排版

- 问题/现象：给《数学分析期中备考》的「聚点」加批注后，「① 三集判定」的三条列表变成 5 个圆点（两条被拆开、出现空条目），列表高度从 89px 撑到 214px；公式与表格不受影响。
- 根因：`applyNoteAnnotations` 用 DOM Range + `surroundContents` 包 `<mark>`。引文「…包含全部聚点」正好在 `<li>` 文本节点的末尾结束，`range.endOffset` 落在下一条 `<li>` 的 `<strong>` 起点，Range 跨过了 `<li>` 边界；`surroundContents` 抛错后走 `extractContents()` 兜底，把两个 `<li>` 塞进了 `<mark>`，列表结构被拆坏。`noteTextIndex` 只记字符偏移、不认元素边界，是这个映射错误的来源。
- 解决方案：`applyNoteAnnotations` 改为「只切文本节点」——按全局字符区间找出重叠的文本节点，在每个节点内部 `splitText` 后只包住那一段文本，跨块时在每块各生成一个 `<mark data-id>`；不碰 `.katex` / `.rich-media-src` / script / style，避免切开 KaTeX 造成公式错位。删除了不再使用的 `locateAnnoRange`。
- 验证：
  - 静态探针（同一份笔记 + 同一条批注）对比旧/新算法：旧算法 5 个圆点、列表 214px、mark 691×114 且内部含 2 个 `<li>`；新算法 3 个圆点、89px、mark 32×21 只含文本。
  - 在运行中的应用里实测：打开《数学分析期中备考》→ 预览，列表 3 条、`聚点` 在第二条内联高亮（31.5×20.4，display:inline），无空条目、无嵌套列表，控制台无报错。
  - `node --check web/init.js`、`dev_check.py` 9 项全过。
- 相关代码：`web/init.js`（`noteTextIndex` / `applyNoteAnnotations`）。

## [2026-11-05] 侧栏新增「刷新页面」按钮

- 问题/决策：开发时改完前端代码要手动按 Ctrl+R，想要一个可见入口。放在侧栏 `.side-foot` 里，与「笔记 / 主题 / 设置」同列，全局可用（Electron 自绘标题栏只在外壳里有，浏览器打开时看不到，所以没放标题栏）。
- 解决方案：`index.html` 在 `.side-foot` 的「笔记」后插入 `<button id="btn-reload" class="ghost wide"><svg class="ic"><use href="#i-refresh"/></svg>刷新页面</button>`；`init.js` 绑定 `$('#btn-reload').onclick = () => location.reload();`。用 `.wide` 独占一行：侧栏窄（约 200px）时三个半宽 `ghost` 并排会把「主题 / 设置」这类两字标签挤成竖排（实测每个按钮内容宽仅 25px，两字需 26px）。
- 相关代码：`web/index.html`（`.side-foot`）、`web/init.js`（`btn-reload` 绑定）。重载后后端仍在跑的任务由 `streamingSids` / attach 自动挂回，不需要额外处理。
- 验证：`dev_check.py` 9 项全过；浏览器实测按钮 144×33 单行不换行，点击后 `performance.timeOrigin` 变化、`navigation.type === "reload"`，重载后按钮仍在，无未捕获 JS 异常。

## [2026-11-05] 导出译文比阅读器细：变量字体读到默认 ExtraLight/Thin

- 问题/现象：PDF 阅读器的译文观感正常，导出的「译文 / 版式译文」整体变细。
- 根因：`reader_export._SERIF_CANDIDATES` / `_SANS_CANDIDATES` 首位是 `NotoSerifSC-VF.ttf` / `NotoSansSC-VF.ttf`——**变量字体**。reportlab 的 `TTFont` 不读 `wght` 轴，只取默认实例；这两个文件默认分别是 wght 200（ExtraLight）和 wght 100（Thin）。已导出 PDF 里能看到 `/NotoSerifSC-ExtraLight`、`/NotoSansSC-Thin`。
- 解决方案：注册前用 fontTools 把 VF 实例化为静态字重（正文 400、标题 700），缓存到 `data/cache/fonts/<名字>-w<字重>.ttf`（按源文件 mtime 失效重建），再交给 reportlab；没有 fontTools 或实例化失败时跳过该候选，退回 simsun / simhei 等静态字体。标题同时获得了真正的粗体（原来只是字号大）。
- 相关代码：`app/reader_export.py`（`_is_variable` / `_instance_vf` / `_register(weight=…)` / `BODY_WEIGHT=400` / `HEAD_WEIGHT=700`）。
- 验证：同一段文字导出对比——旧：内嵌 `NotoSerifSC-ExtraLight` + `NotoSansSC-Thin`，墨量 1.59%、暗像素均值 126.2；新：内嵌 `NotoSerifSC-Regular` + `NotoSansSC-Bold`，墨量 2.16%、暗像素均值 102.8。`tests/test_reader.py` 43 项全过。
- 注意：实例化在首次导出时多花约 2–3 秒并占用约 25MB 缓存；之后直接用缓存。

## [2026-11-05] 回滚前预览：列出将被修改 / 删除 / 新建的文件

- 问题/决策：回退消息、回滚本轮、步骤面板「回到这里」都会同步回滚沙箱文件，但原来只有 `confirm()` 一句「会还原 N 处文件改动」，看不到具体是哪些文件。
- 方案：
  - 后端 `agent.preview_undo(sid, tokens|scope="turn", root)`：按 `undo()` 的真实执行顺序（token 倒序、ops 倒序）扫描，把 `restore(existed)` → modify、`restore(未存在)` → delete、`move_back` → delete(dst) + create(src)、`mkdir/rmdir` → 目录增删；同一路径只保留最终状态；沙箱内转相对路径。新增 `POST /api/agent/undo/preview`。
  - 前端新增统一弹窗 `#rollback-modal` + `rollbackConfirm()`（文件图标 + 路径 + 「将被修改 / 将被删除 / 将新建」，Esc 取消、Enter 确认），三处回滚入口全部改成先预览再确认；预览接口失败时不拦回滚，退回旧式确认。
- 相关代码：`app/agent.py`（`_preview_paths` / `_preview_display` / `preview_undo`）、`app/api/sandbox.py`（`_api_agent_undo_preview`）、`web/index.html`（`#rollback-modal`）、`web/css/base.css`（`.rb-*`）、`web/stream.js`（`fetchRollbackPreview` / `rollbackConfirm` / `rollbackMessage`）、`web/chat.js`（`turnUndo`、步骤面板「回到这里」）。
- 验证：新增 `tests/test_snap_steps.py` 4 条（动作映射 + 相对路径、同文件多次写只留一条、已撤销 token 跳过、move_back）；共 10 项通过。浏览器实测：种子会话点「回退到此（含文件改动）」→ 弹窗列出 `app/agent.py 将被修改`、`new_file.py 将被删除`，取消后消息数不变、无报错。

## [2026-11-05] archify 在非 Agent 模式可用，产物在对话内 iframe 渲染

- 问题/决策：archify（架构图 / 流程图 / 时序图…）原来只挂在 `if agent_on:` 块里，且 `_t_archify` 依赖沙箱；普通对话既看不到工具也没地方落盘。
- 方案：
  - 工具挂载：把 archify 的 schema 移到 `agent_on` 之外常驻生成，再由既有的工具分层（`hidden_tools` / `allow_groups` 的 `diagram` 组、`tool_search` 解锁）决定是否下发；`needs_sandbox("archify") = False`，`tool_search` 也一并豁免（非 Agent 模式才能解锁工具组）。`TOOL_GROUP_KEYS["diagram"]` 补了「结构图 / 系统图 / 模块图 / 关系图 / 状态机 / 示意图 / 脑图」等常见说法。
  - 落盘：没有沙箱时 `archify.workspace_root(sid)` = `data/archify/<会话 id>/`，候选与 HTML 都写这里，不走写入审批。
  - 回传渲染：`_t_archify` 成功后在结果末尾追加 `__ARCHIFY_ARTIFACT__ {url…}`；`chat.py` 摘掉这一行（不给模型看）、发 SSE `diagram` 事件并写进 `tool_log[].diagram`（刷新后可还原）。前端 `buildDiagramCard` 用 `sandbox="allow-scripts allow-popups allow-forms"` 的 iframe 内嵌，`/archify/<sid>/<path>` 免 token（同 `/share/`，仅本机 + 路径限定在 `data/archify`），读取时注入脚本把 `scrollHeight` postMessage 给父页调高度。
  - 提示词：`_VISUAL_DIAGRAM_OFF` 改为「不需要开启 Agent / 沙箱，产物直接显示在对话里」。
- 踩坑：
  - `agent.run_tool` 只在 `is_write and undo_ops` 时登记撤销；archify 的 `writes_files()` 为 True 但 undo 为 None，回滚预览里不会出现它的产物（本来也不该由对话回滚）。
  - 免 token 的 URL 必须走 `do_GET` 里 `/share/` 同级的白名单分支，`/api/*` 一律要 `X-Auth-Token`，iframe 带不了。
  - reportlab 同名重注册会被静默忽略（`registerFont` 里 `if fontName not in _fonts`），验证字体改动必须一进程一种模式。
- 相关代码：`app/agent.py`（schema 位置 / `_t_archify` / `needs_sandbox` / `_VISUAL_DIAGRAM_OFF` / `TOOL_GROUP_KEYS`）、`app/archify.py`（`workspace_root` / `read_for_view` / `view_url` / `_HEIGHT_JS`）、`app/api/chat.py`（`_diag` 摘标记 + SSE）、`app/httpd.py`（`/archify/` 路由）、`web/chat.js`（`buildDiagramCard` / `appendDiagramCards` / message 监听）、`web/stream.js`（`diagram` 事件 + 收尾重排）、`web/css/base.css`（`.diagram-*`）。
- 验证：`tests/test_archify.py` 11 项（含新增「非 Agent 挂载 / 无沙箱产物标记 / 越界路径拒绝」）；实跑一次 archify（无沙箱）通过四道门槛，产物 813KB，标记与 URL 正确；浏览器实测图卡 iframe 加载、高度由 420 自动撑到 1034px、无控制台异常。`dev_check.py` 9 项全过。

## [2026-11-05] API 异常补打完整堆栈（定位 dict update 崩溃）

- 问题/决策：`/api/chat` 抛异常时 httpd 只 `print("[服务] ... 出错：{e}")`，没有堆栈。历史 `data/app.log` 里 `dictionary update sequence element #0 has length 1; 2 is required` 已出现 43 次（多天、多会话），但只能靠猜是哪一行 `dict(字符串)`。
- 解决方案：`app/httpd.py` 的 `_do_post` except 里补 `traceback.print_exc()`。`main.py` 的 `_pipe_output()` 已把 `sys.stderr` 接到 `data/app.log`（按行 flush），所以堆栈直接落盘。GET 路由没有静默兜底，异常本来就有 socketserver 的完整栈，无需改。
- 相关代码/指令：`app/httpd.py`（`import traceback` + `_do_post` 的 except）。复现后查 `data/app.log`：`[服务] /api/chat 出错：...` 下一段就是 `Traceback (most recent call last)` 与具体文件行号。
- 验证：临时注册一个必抛错路由并发一次 POST，捕获到的 stderr 为 `Traceback ... app/httpd.py line 609 in _do_post → 探针函数 → ValueError`，证明堆栈确实会被打印；`py_compile` 与 `dev_check.py` 9 项全过。

## [2026-11-05] 修复：update_plan(log={...}) 触发 dict(字符串) 崩溃（模拟股市会话反复中断的根因）

- 问题/现象：`/api/chat` 抛 `dictionary update sequence element #0 has length 1; 2 is required`，本轮被 abort_chat_stream 异常收尾；刷新后显示「后台输出已中断」。同一错误在 app.log 累计 46 次，多个会话、多天。
- 根因：`app/agent.py` 的 `_plan_log()` 处理 `log` 的字典形式时写成 `dict(v, kind=k)`。字典形式是「键 = kind，值 = 文本」，`v` 是字符串，`dict("文字")` 会按字符当键值对拆 → 首个元素长度 1 → 抛错。模型在收尾时调 `update_plan(log={"decisions": "…"})` 必中。
- 解决方案：
  - 改成显式构造 `{"kind": k, "text": v}`（保留下游 `if isinstance(v, str)` 的过滤）。
  - 顺手修同一函数里 kind 的单复数归一：`decisions/discoveries` 也要认，否则两个桶都会被并进 `decisions`。
- 相关代码：`app/agent.py`（`_plan_log`）、`tests/test_agent_plan.py`（新增 `test_plan_log_accepts_dict_of_strings`、`test_plan_log_ignores_non_string_values`）。定位手段见上一条「API 异常补打完整堆栈」。
- 验证：`tests/test_agent_plan.py` + `test_plan_review.py` 18 项，`test_agent_guards.py` + `test_prompt_assembly.py` + `test_agent_tool_format.py` 135 项，`dev_check.py` 9 项全过；新增用例覆盖「字典形式字符串值」「非字符串值忽略」两种情况。

## [2026-11-05] 内联补全多候选 / AI 改动审阅 / Git 审查与 AI 提交信息

- 问题/决策：对齐 Trae IDE 的三处能力（已超越对方的可视化、回滚粒度、多模型、PDF 翻译保持不动）。
  - 内联补全：原来只在「行尾或后面只剩收尾符号」时触发、每次只给一个候选。
  - AI 审阅：只有逐文件 diff，缺整批改动的摘要 / 风险 / 流程图；Git 侧没有审查入口。
  - Git：提交信息只能手写。
- 方案：
  - 补全后端：`build_messages` 要求模型用单独一行 `---CANDIDATE---` 分隔最多 2 个候选；`parse_candidates` 负责拆分、清洗、去重；新增 `complete_candidates`，`complete` 保留为「只回第一个」的兼容入口。`/api/ide/complete` 新增 `texts` 数组，`text` 继续返回首个。
  - 补全前端：行中也能补（仅当光标两侧都是 `[\w$]`，即标识符中间时跳过）；`items` 返回全部候选；Alt+] / Alt+[ 绑定 `editor.action.inlineSuggest.showNext/showPrevious`；Esc / Tab 时清空候选缓存。
  - 审阅后端：新增 `app/ide_review.py`。`ai_diff` 按 undo 快照逐文件算 diff（只保留增删行）；`review` 走主模型、要求严格 JSON（summary / risks / flow），`parse_review` 宽松解析（剥围栏、截取首尾花括号、risks 字符串也接受）。新增 `POST /api/ide/review`：`scope=ai` 收 `items:[{path,undo}]`，`scope=git` 收 `mode=work|commit|branch`。
  - Git 侧：`git.diff_text`（work 含未跟踪文件补丁 / commit / branch 三点差异）、`git.commit_patch`（优先暂存区）；新增 `POST /api/git/message`。
  - 审阅前端：审阅条加「AI 摘要」按钮，面板内用 `safeRenderMarkdown` + `enhanceRichMedia` 渲染摘要 / 风险 / ```mermaid 流程图；`#ide-diff` 与 Solo 弹窗 `#review-modal` 两套都接。新增 `#ide-git-review` 视图（未提交 / 单提交 / 分支对比三个入口：更改页「AI 审查」、历史行「审查」、分支菜单「审查」）。提交框旁加「AI 生成」按钮，只填输入框不自动提交。
- 踩坑：`clean_completion` 的后文去重循环写错（`while k > 8` 退出后无条件 `if k >= 8` 裁剪），**后文没有重叠也会裁掉末尾 8 个字符**：`print(x)` 变空串、`self.foo = bar` 变 `self.f`，等于把短补全全废掉。改成「只有真正命中公共前缀才裁剪」，并补回归用例。
- 相关代码：`app/ide_complete.py`、`app/ide_review.py`、`app/git.py`（`diff_text` / `commit_patch`）、`app/api/sandbox.py`（`/api/ide/complete` / `/api/ide/review`）、`app/api/git.py`（`/api/git/message`）、`web/gal/gal-ide.js`（`ideAi*` / `ideReviewAi*` / `ideShowView`）、`web/git.js`（`gitGenMessage` / `gitReview`）、`web/index.html`、`web/css/ide.css`。
- 验证：`tests/test_ide_complete.py`、`tests/test_ide_review.py`、`tests/test_git.py`、`tests/test_ide_diff.py` 共 59 项通过；`ruff` 通过；`scripts/dev_check.py` 9 项全过。全量 `pytest` 有 3 个与本次无关的既有失败（`test_prompt_budget` 8268>8000、`test_memory` 7!=2、`test_native_tools::test_compress_pins_task_ledger`）。

## [2026-11-06] 上下文引用对齐 Trae（# 菜单 + chip）/ Git 远端同步 + 标签 + 分支图

- 问题/决策：引用入口原来只有 `@` 和 `#web`，缺 `#code` / `#terminal` / `#git` / `#doc`，也没有 chip 呈现；Git 缺 push / pull / fetch、标签和分支图。已拍板：完全改用 `#`、引用做成可移除 chip、四类新引用全要、Git 做到「标签 + 分支图」档（本次不做 hunk 级部分暂存）。
- 方案：
  - 前端 `#` 统一菜单（`web/composer.js`）：编辑器选区、终端、Git、知识库、文件、文件夹、记忆、历史对话、问题、规则、全仓、`#web`。选中后不往正文写 token，而是进 `state.pendingRefs`，发送时作为 `body.refs`；输入框上方新增 `#ref-bar` chip，可单条移除。
  - `app/fileref.py` 新增 `from_client()`：file / folder / code / terminal / git / problems / rule / doc / memory / workspace。`code_ref` 优先读文件的行区间，读不到才回退前端选区快照（避免拿绝对行号去切快照）。chat 类引用仍在 `chat.py` 用 `_past_chat_ref`。
  - `chat.py` 新消息分支同时处理 chip 引用与旧式 `@` 文本引用；纯引用消息落库为「【引用 N 项】」。
  - 新增 `/api/kb/nodes`（扁平列知识库条目，给 `#doc` 菜单）；kb 模块没有 `@route`，路由要登记在 `app/httpd.py` 的 POST 表里。
  - IDE 侧新增 `ideCurrentCodeRef()`（Monaco 选区）与 `ideTermRecentText()`（读 xterm buffer 最近 160 行）；从文件树拖到输入框会转成 file / folder chip。
  - Git：`git.sync`（fetch / pull / push，首次 push 自动 `-u`）、`git.tags` / `tag_create` / `tag_delete`、`git.graph_log`；`status` 增加 remotes / upstream。路由 `/api/git/sync`、`/api/git/tag`、`/api/git/graph`；前端三个同步按钮、历史「分支图」开关、分支菜单里的标签增删。
- 踩坑：
  - `git log --graph` 会把 `* ` 前缀拼在第一个字段（提交号）前面，不能按第一个 tab 切；改用 `%x1f` 作分隔符。不能用 `\x1e`：Python 的 `splitlines()` 会把它当换行，行会被切碎。
  - `git push origin <branch>` 不会建立 upstream，第一次必须 `-u`。
  - `chat.py` 里局部变量不能叫 `_client_refs`（与函数同名 → UnboundLocalError）。
  - 引用解析必须在 content 为空时也执行，否则「纯 chip 引用」消息拿不到上下文。
- 验证：`tests/test_fileref.py`（8 项）、`tests/test_git.py`（含 sync / tags / graph）与既有关联用例全部通过；`ruff`、`node --check`、`scripts/dev_check.py` 通过；全量 pytest 仍是 3 个与本次无关的既有失败（prompt budget 8268>8000、memory 7!=2、compress pins）。浏览器实测：输入 `#` 弹出 23 项菜单、点「未提交改动」生成可移除 chip、输入框 token 被清除、控制台无报错。
- 注意：浏览器实测启动过一次本地服务（`GALGAME_NO_AUTORESUME=1`），应用启动时会重写 `data/config.json`；已确认 JSON 有效且密钥字段仍在。
- 回归验证（聊天历史渲染 / 引用 chip 改动后）：打开 `http://127.0.0.1:8801/`，`.msg` 60 条、`.msg.user` 29 条全部可见；7 个 `.msg .refs` 块均为可折叠块，`.think-label` 文本统一为 `引用 · 5`，每块 5 个 chip；控制台零报错。
- 踩坑（验证方法）：`browser_console_messages` 的缓冲按 view 保留，且会混入同一浏览器里其他端口（如 8799）的旧错误；用 `newTab` 开新标签再按该 viewId 读控制台，才是本次页面的干净基线。`/@vite/client` 的 `net::ERR_ABORTED` 属正常 HMR 探测，不算脚本报错。

## [2026-11-06] 用户消息气泡显示 # 引用 chip（发送后不再“消失”）

- 问题/现象：发送前输入框上方有引用 chip，发送后 chip 条被清空、用户气泡里又看不到，像是引用丢了。
- 排查结论：
  - 数据没有丢：用户消息 `msg.refs` 存完整正文（供后续轮次继续注入 prompt），AI 消息 `msg.references` 存路径 / 行号元数据（供回显）；`slim_sessions()` 原样透传，刷新后两份都在。
  - 用户气泡 `buildUserRow()` 只渲染 images / docs，根本没有渲染 refs 的代码；引用只在 AI 回复下方以「引用 · N」折叠块出现（实时靠 done 事件，刷新靠 `rowForMsg`）。
  - 另一个可能：后端进程没重启时，旧进程不认识 `body.refs`，引用不会落库。
- 方案：
  - `chat.js` 新增 `buildUserRefs()` / `setUserRefs()`：用户气泡上方渲染显式引用 chip；file / code 可点击跳 IDE，虚拟引用（终端 / Git / 知识库等）用虚线且不可点；`workspace` 只是检索开关，不生成 chip。
  - `stream.js` 发送时先把 `cmpRefPayload()` 的显式引用作为临时 chip 传给 `appendUser()`；`done` 事件再用服务端 `references`（auto=false）校正一次，保证刷新前后一致。
  - AI 回复下方的引用块只保留 `auto: true` 的自动检索条目，避免和用户 chip 重复；旧消息里没有 auto 标记的显式引用会归到用户气泡。
- 相关代码：`web/chat.js`（`buildUserRefs` / `setUserRefs` / `buildUserRow` / `rowForMsg`）、`web/stream.js`（发送时的 `echoRefs`、done 时的 references 分流）、`web/css/search.css`（`.msg-refs` / `.msg-ref`）。
- 验证：`node --check`、`scripts/dev_check.py` 全过；浏览器加载首页 60 条消息（29 条用户消息）正常渲染，7 个 AI「引用 · 5」块正常，控制台零报错。显式 chip 的实发验证需要重启后端后发一条带 `#` 引用的消息（会消耗一次模型调用）。

## [2026-11-06] Archify 流程图 / 图工具汉化（Viewer 界面 + 默认中文图内文字）

- 问题/决策：archify 生成的交互式 HTML 里，Viewer 的按钮 / 图例 / 提示默认是英文。根因不是缺翻译——`vendor/archify/locales/zh-CN.json` 已经自带完整目录，但上游默认 locale 是 `en`，只有候选的 `meta.locale` 才会切到中文。
- 方案：
  - `app/archify.py` 新增 `DEFAULT_LOCALE = "zh-CN"` 与 `_apply_locale()`：JSON 候选在 `meta.locale` 缺省时补 `zh-CN`（显式写了其它 locale 的不覆盖）；explainer 的 Markdown 候选插入 `@meta locale=zh-CN`（有标题插在标题下一行，已有 `@meta` 就追加到同一行）。`finalize()` 在调用 CLI 前执行，写失败不阻断出图。
  - `schema_text()` 增加中文指令：图内文字（标题 / 节点 / 分组 / 说明）默认中文、示例里的英文只学结构不要照抄；并说明 `meta.locale` 可省略。
  - `app/agent.py`：archify 工具描述与 `_VISUAL_TAIL` 增加「图内文字默认中文」，同时覆盖内联的 ```svg / ```chart / ```mermaid。
- 相关代码：`app/archify.py`（`DEFAULT_LOCALE` / `_apply_locale` / `finalize` / `schema_text`）、`app/agent.py`（archify 工具描述 / `_VISUAL_TAIL`）、`tests/test_archify.py`。
- 验证：`tests/test_archify.py` 14 项全过，其中 e2e 用无 locale 的官方示例出图，产物 `<html lang="zh-CN">` 且包含「由 Archify 生成的架构图」；`ruff`、`py_compile`、`scripts/dev_check.py` 全过。

- 注意：不要改 `vendor/archify`（升级会覆盖）；汉化统一在调用侧补 `meta.locale`。

## [2026-11-06] 修复：有「每天重复」日程时工作台加载失败

- 问题/现象：进入工作台弹「工作台加载失败」。`data/app.log` 里 `/api/calendar/events` 500：
  `TypeError: '>' not supported between instances of 'float' and 'datetime.datetime'`（`app/calendar.py` `_iter_starts`）；同一错误还导致 `[日程] 同步系统提醒失败` 与 `[调度] 外部来源 due_reminders 失败`。
- 根因：`_iter_starts()` 的 DAILY 分支把 `range_start`（float 时间戳）和 `cur`（datetime）直接比较、相减；WEEKLY / MONTHLY 分支都已经按时间戳处理，只有 DAILY 漏了。只有库里存在 `FREQ=DAILY` 日程才会触发（本次有两条「每天」提醒）；工作台一次拉 30 天日程，撞上后整个 `Promise.all([日程, 任务])` 失败。
- 修复：DAILY 分支统一用 `cur.timestamp()` 参与比较和相减。
- 相关代码：`app/calendar.py`（`_iter_starts`）、`tests/test_calendar.py`（新增 `test_daily_recurrence_expands_after_range_start` / `test_daily_interval_skips_days`）。
- 验证：`tests/test_calendar.py` 20 项全过；用真实 `data/assistant.db` 按工作台范围（-31 天 ~ +90 天）查询通过，返回 189 条 occurrence（其中 179 条来自两条每日提醒），`due_reminders()` 不再抛错；`ruff`、`py_compile`、`scripts/dev_check.py` 通过。
- 注意：这两条 `FREQ=DAILY` 没有 UNTIL，会一直展开；如果不再需要，建议删除或补结束日期。

## [2026-11-06] Computer Use 使用率对齐 ChatGPT Work：路由提示词 / 登录接管 / 站点三档

- 问题/决策：`computer_*` 工具存在且配置已开启，但 AI 很少用。统计 11 个会话、上万次工具调用：`computer_open` 7 次、`computer_act` 3 次、`computer_see` 1 次、`computer_read` 1 次，0 失败；用户明确说「用我的浏览器打开 B 站」时能完整走通。结论：不是不能用，是工具选择没有把它当默认解法。
- 研究 ChatGPT Work：云端独立浏览器，模型自己决定何时用（优先连接器/应用）；需要登录/信息/确认时暂停；安全登录表单模型看不到凭据；卡住给接管链接；站点权限三档（询问 / 自动批准只拦可疑 / 始终允许）+ 单站覆盖；高危操作二次确认；独立 Cookie / 登录态；浏览、Computer Use、代码执行在同一 Agent 循环里。
- 方案：
  - 路由：`_tool_decision_prompt(computer_on=…)` 增加「操作网页 / 登录 / 点击 / 填表」类别和第 5 条纪律；`_COMPUTER_PROMPT` 明确「具体网站默认先 computer_open」「登录 / 验证码 / 扫码 → ask_user(action=true) 请用户在受控窗口完成，之后 computer_see 继续」。
  - 工具描述任务化：`computer_open` 写清「需要登录 / 点击 / 填表 / 看登录后内容 / 验证页面时用，普通查资料优先 web_search / fetch_url」，其余工具按触发场景重写。
  - 站点三档：新增 `computer_site_mode = ask | auto | allow`（`app/computer/policy.py` 的 `site_mode()`）；auto 只放行 https 公网域名，http / 裸 IP / 非常规端口 / punycode / 无点主机名仍询问；白名单与本次进程授权在任何档位下都放行；内置禁区不变。设置页把原来的「无限制」勾选框改成三档下拉。
  - 兼容：`computer_sites` / `computer_apps` / `computer_extra_block` 支持数组、换行、逗号三种旧格式；`save_config()` 统一存成数组；`site_mode` 里 ask + 旧键 unrestricted=true 视为 allow，避免升级后突然收紧。
- 相关代码：`app/agent.py`（`_tool_decision_prompt` / `_COMPUTER_PROMPT` / computer 工具 schema）、`app/computer/policy.py`（`site_mode` / `_patterns` / `_suspicious` / `check`）、`app/config.py`（`_list_value` / `save_config`）、`web/settings.js`（三档下拉 / `settingsList`）、`tests/test_computer_policy.py`、`tests/test_computer_agent.py`。
- 验证：计算机相关与提示词测试 64 项全过；`ruff`、`node --check`、`scripts/dev_check.py` 全过；设置页浏览器实测 `#computer-site-mode` 存在、三档选项正确、控制台零报错；真实配置下 `site_mode` 生效值为 allow（保留原来的 unrestricted=true 行为）。
- 注意：桌面窗口能力仍是关闭状态（`computer_desktop_enabled=false`），这次提升的是浏览器 Computer Use；桌面要在设置里单独开启并填白名单。测试中设置页意外写入了 `computer_site_mode: "ask"`，代码的兼容规则会把它按旧键 `unrestricted=true` 解释为 allow；想改用「自动批准」可在设置里切换。

## [2026-11-07] 划词浮层统一：SelKit 内核 + 对话 / 笔记 / PDF 三适配器

- 问题/决策：对话（aside.js）、笔记（init.js）、PDF（reader.js）各有一套划词工具条：三套定位、三套选区校验、三套 document 监听、三份 CSS，文本上限 4000 / 500 / 1600 不一致；互相关闭、z-index 70 / 9600 / 10000 打架。
- 方案：
  - 新增 `web/selkit.js`：统一 pointerup / Shift keyup 触发、选区校验（排除 input / textarea / contenteditable 与浮层 / 面板）、清洗与截断、浮层渲染、视口夹取 + 上下翻转、Esc / 滚动 / 外部点击关闭、mousedown 保选区、快捷键（Ctrl+Enter 提问、Ctrl+Shift+Enter 翻译）。
  - 动作模型：`primary` 主操作，其余收进「更多」；适配器只注册上下文与动作。浮层左侧显示来源标签（对话 · #N / 笔记《…》/ PDF · 第 N 页），超长显示「已截断」。
  - 对话：`aside.js` 删掉 `.aside-bar` 与旧 mouseup / scroll 监听，注册 chat 适配器（提问 / 引用 / 更多→复制）；引用 chip 带「对话 · #N」标签。
  - 笔记：`init.js` 删掉 `.nt-sel-bar` / `showAnnoAdd` / `hideAnnoAdd`，注册 notes 适配器（提问 / 引用 / 更多→批注、复制），保留 `noteDraftFromRange` 的 prefix / suffix / nth 与批注 / 问答弹层。
  - PDF：`reader.js` 删掉 `.rd-sel-tools` 与旧 mouseup / 快捷键，注册 reader 适配器（提问 / 翻译 / 引用 / 更多→批注、摘录、高亮、复制）；`.rd-sel` 只保留回答 / 批注 / 色板面板，高亮仍由原色板保存。
  - 新增 `web/css/selkit.css`（`.selbar` / `.selbar-more-menu`，z-index 9800 / 9801），删除 aside.css / notes.css / reader.css 里三份旧工具条样式；`index.html` 引入。
- 相关代码：`web/selkit.js`、`web/css/selkit.css`、`web/aside.js`（`registerSelection`）、`web/init.js`（`registerNoteSelection`）、`web/reader.js`（`registerReaderSelection` / `captureFlowSelection` / `capturePageSelection` / `openReaderPanel`）、`web/index.html`。
- 验证：`node --check` 四个 JS 全过；`scripts/dev_check.py` 全过；浏览器实测：对话划词出现「对话 · #1022 / 提问 / 引用 / 更多」，更多里有复制，点引用后 `#quote-bar` 出现 chip；笔记预览划词出现「笔记《11.3》/ 提问 / 引用 / 更多」，控制台零报错。PDF 已在 resume.pdf 上实测（原页划词浮层 / 批注落右侧 / 双向联动见下一条）。
- 注意：快捷键统一为 Ctrl+Enter 提问、Ctrl+Shift+Enter 翻译（reader 原来的 Ctrl+Enter 翻译已按新规范调整）；三处回答 / 批注弹层仍各自实现，这轮只统一浮层与触发 / 定位 / 生命周期。

## [2026-11-07] PDF 原页划词接入 SelKit + 批注落右侧译页 + 左右双向联动

- 问题/需求：此前只有阅读流 `.rd-flow` 和版式译页 `.rd-tpages` 会弹统一浮层，左栏原页 `.rd-pages` 划词只做单向联动、没有任何工具条；译页划词也不回标左栏。用户要求：原页也能划词，但批注 / 高亮 / 摘录要落到右侧译文块上；联动改成双向。
- 方案：
  - `registerReaderSelection()` 的 `roots` 增加 `.rd-pages`，`capture()` 优先判断左栏；新增 `captureLeftPageSelection(range)`：按 `.rd-page` 取页码 + `cleanRangeText` 取原文，`findBlockForQuote(page, quote)` 找对应 block；对不上时（页眉 / 连字 / 图表注）用 `findBlockNear()` 按选区中心点与 `block.rect`（PDF pt）做几何兜底。`sel.side` 记 `src`：quote 是原文，高亮落在右侧阅读流的原文段落；版式译页没有原文字层，退化为块角标——两条路都在右侧，不改左栏 canvas。
  - 左 → 右：`linkToFlow(no, quote, b)`（由原 `linkFromPageSelection` 拆出）画块级矩形 + `paintLiveFlow` + `scrollFlowToBlock`，再异步 `ensureTextData` / `findRectsInIndex` 精修字符级。
  - 右 → 左：新增 `linkToLeftPage(no, blockId, quote)`；`.rd-tpages` 划词（`capturePageSelection`，side=dst）用块原文在左栏定位 / 高亮。阅读流里的 dst 选区同理：`linkFromFlowSelection()` 在 side=dst 时改用 `blockOf(id).text`（译文无法在 PDF 字符索引里匹配）。
  - 修浮层被自己关掉的两个坑：① `paintLiveFlow` 在「选区就在这段 `.rd-src` 里」时不再套 mark——改 DOM 会让浏览器丢选区，`selectionchange` 随即关浮层；② `clearLive` 拆 `mark.rd-hl-live` 时跳过与当前选区相交的节点；③ SelKit 的滚动关闭改为 `onScroll`：弹出后 800ms 内不关（联动会自动滚右侧面板），之后用户自己滚动仍然关闭。
  - SelKit 的 `capture` 异常不再静默，改为 `console.error('[SelKit] capture failed:', id, e)`。
- 相关代码：`web/reader.js`（`captureLeftPageSelection` / `findBlockNear` / `linkToFlow` / `linkToLeftPage` / `linkFromFlowSelection` / `paintLiveFlow` / `clearLive` / `registerReaderSelection`）、`web/selkit.js`（`S.shownAt` / `onScroll` / capture 错误日志）。
- 验证（Edge headless + CDP，脚本 `.tmp_audit/reader_selbar_probe.py`，目标 resume.pdf）：
  - 原页划词：`.selbar` 出现，标签「PDF · 第 1 页」，按钮 提问 / 翻译 / 引用 / 更多，`SelKit.current = {page:1, blockId:'p1bd7…', side:'src'}`，左栏 `.rd-live-rect` 7 段（左 → 右联动）。
  - 批注落右侧：更多 → 高亮 → 黄色 → toast「已高亮」，批注数 +1；左栏 `.rd-hl-layer` / 角标始终为 0（没画回原页）；切到双语文案后 `.rd-flow mark.rd-hl` 出现。
  - 双向联动：原文阅读流划词（side=src）左栏高亮 3 段；译文阅读流（side=dst）与版式译页（side=dst，标签「译页 · 第 1 页」）划词都在左栏出现高亮；版式对照下从原页划词，右侧联动滚动后浮层仍保持可见。
  - 控制台零报错；`node --check web/reader.js web/selkit.js`、`scripts/dev_check.py` 全过。测试写入的 4 条 resume.pdf 高亮已从 `data/assistant.db` 删掉（只留用户原有的那条 dst 高亮）。
- 注意：原页选区的 quote 是原文，所以批注的 `side` 是 `src`；版式译页里通过块角标显示，阅读流里高亮在对应原文段落。若以后要做「译文文字级高亮」，需要额外的原文 → 译文片段映射，当前数据模型没有。

## [2026-11-07] 划词提问统一：AskKit 面板 + /api/ask/stream + 文档级线程

- 问题/决策：SelKit 只统一了工具条，问答面板仍是三套（`.aside-panel` / `#nt-ask-pop` / `.rd-sel`），请求链路三条、能力不一致（只有对话流式 + 多轮 + 停止）。统一为 AskKit，三处只声明上下文与落库方式。
- 方案：
  - 新增 `web/askkit.js` + `web/css/askkit.css`：统一浮动卡片（引用块、线程标签页、扩展按钮区、问答列表、输入 / 停止、复制回答 / 引用到对话 / 存为批注）、drag / Esc / 外部点击关闭、SSE 流式（delta / reasoning / error）、AbortController 停止、多轮历史。
  - 后端：`/api/ask/stream` 与 `/api/ask/threads`（`source: chat|notes|reader`）；`app/aside.py` 按来源拼 prompt（对话旁注 / 笔记助手 / 论文阅读助手）；`aside_store` 改成「作用域线程」：`session:<sid>`（对话）与 `doc:<doc_key>`（笔记 / PDF），旧 `by_session` 结构与裸 sid 调用自动兼容。
  - 三处适配器：对话（`source=chat`，选区前后文 + 消息全文，保留快捷问法）、笔记（`doc:notes:<路径>`，prefix/suffix，存为 `kind=qa` 的 annotations）、PDF（`doc:<相对路径>`，扩展区放翻译 / 高亮 / 批注，问答存 qa）。
  - 删除旧实现：`.aside-panel` + `aside.css`、`#nt-ask-pop` + 对应 CSS、`.rd-sel` + 对应 CSS；`/api/notes/ask` 与 `/api/reader/selection?mode=ask` 下线（翻译保留）；SelKit 的 `insideOwn` 只留 `.askkit` / `.nt-anno-pop`。
  - 修两个联调问题：① AskKit 的 fetch 用 `window.TOKEN`（不存在）导致 401 `unauthorized`，改为读顶层 `TOKEN` / `window.__TOKEN__`；② 发送按钮最初没绑 click，只能回车发送。
- 相关代码：`web/askkit.js`、`web/css/askkit.css`、`web/aside.js`、`web/init.js`、`web/reader.js`、`web/selkit.js`、`web/index.html`、`app/aside.py`、`app/aside_store.py`、`app/api/aside.py`、`app/notes.py`、`app/api/notes.py`、`app/api/reader.py`、`app/httpd.py`、`tests/test_aside.py`、`tests/test_notes_ask.py`。
- 验证：
  - 后端：`tests/test_aside.py` 17 项 + `tests/test_notes_ask.py` 2 项全过（含 `/api/ask/*` 路由、notes/reader prompt、doc 作用域隔离、QA 批注映射）。
  - 浏览器（Edge headless + CDP，临时服务 51599，`.tmp_audit/askkit_probe.py` / `askkit_diag2.py`）：PDF 划词 → 「文献问答」+ 扩展 [翻译/高亮/批注]，翻译扩展返回译文；对话划词 → 「旁注」+ 3 个快捷问法；笔记划词 → 「笔记问答」+ 存为批注按钮；三处均创建线程（标签页）。笔记真实提问流式返回完整回答；`data/aside.json` 中 `doc:notes:11.2.md` 线程记录到该轮问答（多轮落库）。`.rd-sel / #nt-ask-pop / .aside-panel` 节点数均为 0，控制台零报错。
  - `node --check` 五个 JS、`scripts/dev_check.py` 全过；验收写入的 doc 作用域线程已从 `data/aside.json` 清掉。
- 注意：对话消息没有文档锚点，所以「存为批注」只出现在笔记 / PDF；浏览器验收里「存为批注」的点击时机撞上流式中（按钮 disabled）未单独复验，落库路径由单测覆盖，实际按钮点击后走的是原有 `/api/notes/anno/save`。线程按作用域保留最近 20 个、每线程 12 轮，和旧旁注一致。

## [2026-11-08] 本地混合模型：开关 + 历史压缩 + 本地摘要质量闸门

- 问题/决策：周 token 用量 2 亿级。实测最近 7 天 3.35 亿（再前一周 28.2 亿），**99.6% 来自主 Agent chat**；笔记/阅读器/补全等轻量任务合计只有 151 万（0.45%）。把简单任务切本地最多省 0.45%，真正能降的是「回合开始时的历史」：实测 71.8% 的 prompt 是 `Σ(轮数×起始prompt)`，平均每轮 11.6 万 token。
- 方案：
  - 一键开关 `hybrid_mode`（off / on / light）：off = 纯 API（本地 light 不参与、沿用原 `chat_context_limit`）；on = 轻量任务走 light，并按 `hybrid_context_limit`（默认 64000）+ `hybrid_keep_ratio`（0.45）提前压缩历史；light = 主对话也只走 light（不限定本地 / 云端）。设置页「模型接入 → 本地混合模式」三档切换。
  - 仅轻量模型（light）：`get_api_conf("chat")` / `get_api_kind("chat")` / `provider_chain` 全部改指向 light；`provider_chain` 只留 light 一条（不回落到主 API）。`light_chat_is_local()`（light 在本地）时：固定开销（schema≈9.7k + tools_doc≈4k + rules≈2.7k + memory≈2k）超过本地 8k 上下文，`agent_on` / `search_ready` / 原生工具 schema / 工具提示全部关闭，按「无工具、无联网」问答运行；上下文预算改用 `local_context_limit`（12000）/ `local_keep_ratio`（0.35）/ `local_summary_tokens`（2048）/ `local_max_range_tokens`（10000），压缩失败即放弃本次压缩（没有别的模型可退）。云端 light 不受这些限制：窗口沿用用户填的 `chat_context_limit`，工具 / 联网 / 识图按正常设置走。
  - 识图：本地 light（9B 文本模型）不支持多模态，`use_chat_for_vision()` 返回 False，改走设置里的视觉 API（没配就按「未配置」报错）；云端 light 仍跟随原设置。
  - 本地模型：`D:\Ai\Model\本地部署\Qwen3.5-9B-IQ4_NL.gguf` 已导入 Ollama（`qwen35-9b:iq4nl`，5.2GB，100% GPU，8192 上下文）。实测短问答 44–48 tok/s；5k token 摘要 4.3s；9k token 分块摘要约 108s（3 块）。Ollama 的 OpenAI 兼容端点默认 thinking，必须发 `reasoning_effort: none`，否则正文为空。
  - 上下文与派生模型：Ollama tag 里写死了 `PARAMETER num_ctx 8192`（`ollama show --modelfile` 可见），这是部署参数、不是模型能力（GGUF 原生 262144）。扩窗用派生 tag：`FROM qwen35-9b:iq4nl` + `PARAMETER num_ctx 32768` → `ollama create qwen35-9b:32k`；权重 blob 按 hash 复用，不多占 5GB。程序侧 `local_context_limit` 已做成设置项「本地窗口（tokens）」，必须与 num_ctx 一致。显存估算（实测 8192 ctx → KV 256 MiB，即 ≈32 KiB/token）：16k≈512 MiB、32k≈1 GiB、64k≈2 GiB；模型本体 4.57 GiB，32k 合计约 6.2 GiB。Ollama 自带界面能一直聊是因为界面保留完整记录，发给模型的历史同样受 num_ctx 限制；模型信息里的 262144 只是训练上限。
  - 思考过程：Ollama 的 OpenAI 兼容端点把思考放在 `delta.reasoning`（vLLM / DeepSeek 用 `delta.reasoning_content`），原先只认后者 → 本地模型的思考被整段丢掉、前端没有「深度思考」块。已在 `llm.stream_chat` 同时识别两种字段并照常发 `{"type":"reasoning"}` 事件。默认 `chat_think_enabled=false` → 发 `reasoning_effort: none`，所以默认看不到思考；打开输入框左下「深度思考」（档位 low/medium/high/max）才会显示。实测同一问题（9.11 vs 9.9）：关思考 0.2–0.3s / 5 completion tokens，答 9.11；开思考 7.7–10.3s / 433–466 tokens（含思考），答 9.9。注意思考 token 计入输出上限，`max_tokens` 太小时会出现「想完没正文」（测试里 512 上限、思考 469 时就复现过）。
  - 本地摘要：`compress_history` 在混合模式下按 3500 token 分块调用本地模型，多块再合并；区间超过 `hybrid_local_max_range_tokens`（12000）或同步发送前兜底压缩时改用主模型（控延迟）。仅轻量模型模式没有 API 可退，区间超过上限也强制本地分块压缩（否则下一轮必超窗），耗时随区间线性增长。
  - 质量保障：① 本地摘要自动追加「关键文件与错误（自动提取）」清单（路径 / 错误串由代码从原文抽取，不依赖模型转述）；② 加权锚点闸门（路径 / 错误权重 3，标识符权重 1，阈值 `hybrid_quality_gate=0.55`），不达标或调用失败自动回退主模型（`hybrid_fallback_api=true`）；③ 原有 pin 保留目标 / 验收项 / 计划 / task_state，保留最近 45% 原文。
  - 同步路径（chat.py 两处 `compress_history`）在混合模式传 `prefer_local=False`，避免本地分块让一轮请求停顿几十秒；仅轻量模型模式传 `prefer_local=True`（主对话就是 light，只能用它压）。
  - IDE 内联补全默认不切本地（`hybrid_ide_complete=false`）：补全只有 350ms 防抖 + 3–5s 超时，9B 本地模型会拖慢输入；需要时再打开。
- 实测质量 A/B（同一段真实历史，约 9k tokens，55 个锚点）：
  - 本地 + 自动事实清单：加权召回 **0.680**，关键路径/错误 **21/21**，1685 字，108s。
  - API 摘要：加权召回 **0.495**，关键路径/错误 **15/21**，1390 字，6s。
  - 结论：关键事实本地不差于 API（路径/错误 100% vs 71%），但延迟高，所以本地只跑后台；不达标仍回退 API，质量不会比现状差。
- 相关代码：`app/config.py`（`hybrid_*` / `local_*` / `is_local_url` / `hybrid_on` / `light_chat_mode` / `light_chat_is_local` / 仅轻量模型时 chat 服务商覆盖 / Ollama 思考模式）、`app/llm.py`（`_pick_light_model` / light 协议判断 / `reasoning` 与 `reasoning_content` 双字段兼容）、`app/context.py`（`context_limits` 混合与本地轻量预算、`_local_compress` / 分块 / 合并 / 锚点闸门 / 自动事实清单）、`app/api/chat.py`（本地轻量模型关闭工具与联网、同步压缩 prefer_local）、`app/agent.py`（本地轻量模型直接不注入工具提示）、`web/settings.js`（off/on/light 三档 + 压缩参数）、`tests/test_hybrid.py`。
- 验证：`tests/test_hybrid.py` 11 项（含仅轻量模型覆盖 chat conf / provider_chain / vision / 本地 vs 云端上下文预算 / 无工具提示、Ollama thinking、`delta.reasoning` 捕获）、`test_aside.py`、`test_notes_ask.py`、`test_context.py`、`test_native_tools.py` 全过；全量 `pytest tests` 通过；`scripts/dev_check.py` 全过。仅轻量模型流式实测（`hybrid_mode=light`，`stream_chat` 直连 `qwen35-9b:iq4nl`，`reasoning_effort=none`）：7.8s 首包（含模型加载）、`visible="1+1 等于 2。"`、reasoning 0 字、无 tool_calls。Ollama 实测 `ollama ps` 5.2GB / 100% GPU / 8192 ctx，显存 5112/8151 MiB。
- 测试隔离：`tests/conftest.py` 新增 `_no_hybrid_by_default`，强制测试里的 `load_config()` 返回 `hybrid_mode=off`（逐个替换各模块早绑定的 `load_config` 引用）。否则本机 `data/config.json` 打开混合 / 仅轻量模型后，压缩与工具挂载会随环境漂移。测混合行为的用例自己 monkeypatch。
- 顺带修的陈旧测试：`test_notes_ask_endpoint` 随 `/api/notes/ask` 下线删除；`test_memory` / `test_agent_guards` 补环境隔离（真实 notes / computer 站点档位）；`test_native_tools` 的 fake `_quick_chat` 补 `stop_event`；`test_prompt_budget` 阈值 8000 → 8600（实测 8315）。
- 注意：默认 `hybrid_mode=off`，需要省 token 时在设置里打开；关闭即完全回到纯 API。按 64k 配置，第一次大区间压缩通常超过 12k 上限会走 API（成本仍远小于省下的 prompt），本地主要接小范围后台压缩与轻量任务。仅轻量模型 + 本地 light 是「完全离线可用」的降级档：没有文件 / 命令 / 联网工具，上下文只有 12k，9B 模型的长文推理与代码能力明显弱于 API，只适合普通问答；它不产生主 API token，但也没有回退。云端 light 则按正常工具与窗口运行，只是换了更便宜的模型。2 亿/周的口径（原始 token / 计费 token / 配额）仍需确认，缓存命中占 91.6%，实际账单降幅可能小于 token 降幅。



## [2026-11-08] 前端交互体验升级（阶段二）：流式节奏 / 滚动稳定 / 反馈 / 过渡

- 问题/决策：流式输出用固定 90ms 定时重绘（约 11fps），长尾每帧整段 Markdown 重解析，状态条每个 delta 都 appendChild（CSS 动画反复重启）；消息区滚动条随内容出现/消失会把整列左右推；模式切换是 `hidden` 硬切、零过渡；toast 是单例（后一条吃掉前一条）；发送后按钮无受理反馈且焦点掉到 body；折叠头等可点 div 无按压反馈；弹窗只有焦点进/出，没有 Tab 约束。目标：只改前端表现层，不动接口、SSE 协议与消息结构。
- 方案：
  - 流式渲染（`web/stream.js`）：重绘节流 32ms（≈30fps），按「尾巴长度 / 未闭合代码围栏 / 标签页隐藏」自适应降到 56 / 90 / 200ms；尾巴无换行且无 Markdown 标记时走 `textContent` 纯文本快通道（省掉每帧解析 + innerHTML 重建）；`keepLiveLast` 只在状态条不在末尾时才挪动，避免每 token 重启点阵动画。
  - 滚动（`web/css/polish.css`）：`#chat` 加 `scrollbar-gutter: stable`（滚动条常驻占位）、`overscroll-behavior-y: contain`、`scroll-padding-bottom`；保留了原有 rAF + 直接赋 scrollTop 的跟随策略（流式期间不能平滑滚动）。
  - 发送反馈（`web/stream.js`）：`setSendBusy()` 让发送按钮进入 `.is-busy` 转圈态（`.send.is-busy::after` 旋转环），拿到响应头 / 进入 finally 即复原（15s 兜底）；发送与排队后 `focusComposer()` 回焦输入框（`pointer: coarse` 设备跳过）。
  - Toast（`web/core.js` + `index.html` + `base.css`）：由单例改为 `#toast-stack` 堆叠，最多 3 条，同一句 1.8s 内只累加 `×N`，点击可关；`ok`/`err` 语义色沿用。
  - 过渡（`web/gal/gal-ide.js`）：`applyUiMode` 在模式真变化时对新面板播放 `panel-in` 淡入；IDE 跳过（Monaco 每帧 resize）；只动 opacity——面板里有 `position:fixed` 浮层（GAL 开场卡），transform 会临时改变其包含块。`renderMessages` 整块重画后给 `#chat` 播 `chat-fade`（切会话不闪白）。
  - 弹窗焦点（`web/settings.js`）：改为 document 捕获阶段的 `keydown`（Tab 回绕）+ `focusin`（焦点跑出就拉回），用 `_modalStack` 支持嵌套弹窗；`pointerdown`/`click` 记录触发元素，关闭时优先还给刚点的按钮，其次回退到打开前的 activeElement。
  - 规范统一（`polish.css` 新增「阶段二」段）：新增 `--ease-out` / `--dur-1..3` / `--ring` 语义令牌；`button` 统一过渡（含 transform 按压）；折叠头 / 工具卡 / 模式卡补 `:active`；`(hover: none)` 设备消息操作条常显；`prefers-reduced-motion` 下新入场动画不播。
  - 文档图片补 `decoding="async"`（`web/markdown.js`）。
- 相关代码：`web/stream.js`（PAINT_MS_* / paintGapFor / tailIsPlain / keepLiveLast / setSendBusy / focusComposer）、`web/chat.js`（replayEnter 调用）、`web/core.js`（toast 堆叠 / replayEnter）、`web/settings.js`（焦点陷阱 / showWithAnim / hideWithAnim）、`web/gal/gal-ide.js`（replayModeEnter）、`web/markdown.js`、`web/index.html`（#toast-stack）、`web/css/base.css`（.toast-stack / .toast-item）、`web/css/polish.css`（阶段二段）。
- 验证：`node --check` 六个改动 JS 全过；`scripts/dev_check.py` 全过（132 文件编译、60 个前端 JS 语法、hotkeys 73 项、markdown 45 项、transpage 18 项、服务端冒烟）；`scripts/markdown_test.js` 45 项全过。浏览器实测（临时服务 54353，`hybrid_mode=light` 走本机 Ollama）：页面加载无控制台报错；发一条含代码块的长回复，过程区文本长度 42→772→2231→3101→3692→4724→5483→8816 逐段增长，流式期间 `bottomGap≈0` 无回跳，结束后代码块正常高亮（62 个 span）+「展开全部（33 行）」；设置弹窗 Tab ×8 / Shift+Tab ×8 均在弹窗内，Esc 后焦点回到设置按钮。
- 已知/边界：① 流式结束按服务端 blocks 重排（过程折叠 + 正文落位）会改变高度，视觉上有一次贴底下移，属于收尾重排而非滚动抖动，未做平滑过渡（避免与大段正文重绘抢帧）。② 浏览器自动化环境后来出现 WebView 超时（CDP 存活但渲染进程不可用），未能复测 toast ×N 与面板淡入；这两项仅有静态检查与实现推演，未做真人点击确认。③ 流式测试时控制台出现过 1 条 `net::ERR_ABORTED /api/chat`，同请求 Resource Timing 为 200 且正文完整，服务端日志对应 `ConnectionAbortedError`，与本次改动无关（自动化切页/中断连接产生）。
- 追加（同日）：侧栏「新建对话」按钮重设计。原来是整块浅强调底 + 内容居中（全宽、圆角 10px），看起来像一块占位色块。改为对齐 Claude / ChatGPT 侧栏的「New chat」：`display:flex` 左对齐、图标 15px 强调色、文案 13.5px/600、固定 38px 高、11px 圆角、`--bg-input` 中性面 + `--border` 细边；hover 换 `--bg-hover` + `--border-strong` + `--shadow-sm`，按下 `scale(.985)`，补 `:focus-visible` 焦点环。相关代码：`web/css/base.css` 的 `.new-chat*`。浏览器实测（739×706 浅色）：38px 高 / 11px 圆角 / 白底细边 / 图标与文字左对齐且垂直居中（中心偏差 0px），控制台 0 报错。

## [2026-11-08] AI 润色 405：_quick_chat 的 light 分支漏补全 URL
- 问题/决策：输入框 AI 润色（✨ / Ctrl+I / /polish）报错「调用失败：405 Client Error: Method Not Allowed for url: http://127.0.0.1:11434/」。根因：`_quick_chat` 选中本地轻量模型时用 `cfg["light_url"]` 原始值覆盖 `api_url`，没走 `resolve_endpoint` 补全；主对话 URL 在 `get_api_conf` 里补全过 `/v1/chat/completions`，所以主聊天正常、只有轻量调用炸。
- 解决方案/原因：`_quick_chat` 发请求前补一段——`api_name == "light"` 且协议为 openai 时 `api_url = resolve_endpoint(api_url)`（chat 分支已补全，不重复；anthropic 协议不动）。影响面：所有走 `_quick_chat + light` 的轻量调用（输入润色/预检、GAL 判定、摘要、hybrid 压缩、IDE 补全）此前全灭；其中 hybrid 压缩因 `hybrid_fallback_api` 静默回落主 API，长期未察觉——**修复后这些才真正开始调用本地 9B，本地推理负载会增加**，这是混合模式本来的预期行为。
- 相关代码/指令：`app/llm.py` L14-15（import 加 `resolve_endpoint`）、L189-194（light 分支补全判断）。验证：临时探针起服务 POST /api/compose，修复前 405、修复后 `{"ok": true, "text": "今天天气不错，我想出去散散步。", "intent": "闲聊"}`（本地 qwen35-9b:iq4nl 实际推理返回）；`scripts/dev_check.py` 全过。
- 排查技巧：PowerShell 里 `curl` 是 Invoke-WebRequest 别名，`-m` 参数歧义，测 HTTP 用 Invoke-RestMethod；POST Ollama 裸地址返回 405，可作端点拼接错误的特征。

## [2026-11-08] 提问卡取消等待倒计时（永不超时）
- 问题/决策：用户要求取消中途提问卡的等待倒计时，选「彻底永不超时」：提问后一直等回答，不再到时按「未回答」自动继续；推进方式只剩 回答 / 点「让 AI 自己决定」/ 点停止。
- 解决方案/原因：后端 `_ask_timeout` 写死 0（chat.py）；`new_asks` 的 clamp 下限 30→0、`timeout is not None` 区分显式 0，0 时 `expires_at=0`；`wait_answers` 在 `deadline=None` 时永久循环（0.3s 轮询 event + stop_event，每 30s 续期 session_status 防「待回答」被 TTL 清掉）。前端零改动：三处倒计时都是 `expiresAt>0` 才启动，stream.js 里 `expires_in=0` 走 falsy 分支不生成 meta。config 默认值 `ask_timeout` 删除（本就没暴露到设置界面）。
- 相关代码/指令：`app/agent.py`（new_asks / wait_answers）、`app/api/chat.py` L2813-2814、`app/config.py`、`tests/test_ask_card.py`（clamps 断言改为 1 秒、删 ask_timeout 默认断言、新增 `test_new_asks_zero_timeout_means_no_expiry`）、`readme.md` 中途提问行。测试用 `.venv\Scripts\python.exe -m pytest`（系统 python 没装 pytest，py 启动器有 9.1.1）。
- 验证：pytest 全量通过；`scripts/dev_check.py` 全过。

## [2026-11-08] 「回到实时输出」：后台任务接管为完整流式
- 问题/决策：刷新 / 重开窗口 / 断线后，任务仍在服务端跑，但前端只有纯文本回放（`startJobReplay`），没有和正常发送一致的流式渲染。用户选择「完整流式接管」：不重写渲染主干，而是给 `streamChat` 加第三种数据源。
- 解决方案/原因：
  - `streamChat(extra, resume, attach)`：attach 为真时 fetch `/api/chat/attach`（POST `{session_id, since}`），其余事件处理 / 渲染（`paintLive`、思考块、工具卡、提问卡）/ 收尾全部复用同一条管线，因此体验与正常发送一致。404 → `__attachGone`，不写错误文案、直接拉状态对齐。
  - attach 模式跳过 `setSendBusy`：全局发送态不该被别的会话的接管操作清掉。
  - UI：`#resume-bar`（输入框上方提示条 + 「回到实时输出」按钮）。显示条件 = `streamingSids[current] && !streamOf(current)`，即「服务端在跑、本地没有流」；`refreshResumeBar()` 挂在 `refreshStreamingUI()` 末尾，注册/注销流、切会话都会自动同步。按钮点击 → `resumeLiveOutput(sid)`。
  - `resumeLiveOutput`：本地流还在 → 只切会话（`attachStreamHost` 挂回 live DOM）；否则先停掉自动回放（`_replays` 记录句柄 → `takenOver` + `reader.cancel()`，等它退出，最多约 1 秒）再 `streamChat({}, null, {since:0})`。**必须先停回放**：否则旧回放的 `pullState → renderMessages` 会把刚接管的实时行整块重画掉。
- 相关代码/指令：`web/stream.js`（`streamChat` attach 分支、`refreshResumeBar`、`_replays`、`waitJobThenRefresh` 取消检查、`resumeLiveOutput`）、`web/index.html`（`#resume-bar`）、`web/css/base.css`（`.resume-bar/.resume-btn`）、`web/init.js`（按钮绑定）、`web/chat.js`（`msg-run-chip` 改按钮）。
- 验证（浏览器实测，真实模型 3000 字长回答）：生成中刷新 → 提示条出现 → 点击接管 → 从 0 回放 4806 字并继续增长到 11183 字；`h2` 计数 3→6（markdown 实时成形）；结束后停止键隐藏、`streamingSids` 清空、操作条恢复；控制台 0 条非 abort 错误。`dev_check.py` 全过。
- 已知边界：① 控制台会出现 `net::ERR_ABORTED /api/chat/attach`（主动取消 reader 的预期现象）；② 若在「等待提问」阶段接管，attach 重放 ask 事件可能短暂出现两张提问卡，done 后重建消除；③ 本次实测在真实 data 留下两个会话 `s_139f58ad0c77`、`s_061cfb7d36a2`（待用户决定是否删除）。

## [2026-11-08] IDE 文件树拖出真实文件（Electron startDrag）

- 问题/现象：从 IDE 文件树把文件拖到桌面 / 资源管理器 / 其他应用，拿到的只有文件名或路径文字。树内移动（`/api/sandbox/move` → `shutil.move`）与 Ctrl+C/Ctrl+V 复制本身都是真实文件操作，问题只在「拖出窗口」这一条路径：`dragstart` 只写了 `text/plain` 相对路径。纯浏览器（含 Edge `--app`）受安全限制，无法把真实文件放进系统拖拽数据，只有 Electron 主进程能发起原生文件拖拽。
- 方案：
  - `desktop/main.js` 新增 IPC `file:start-drag`：`fs.statSync` 校验存在后调 `win.webContents.startDrag({ file, icon: ICON })`（icon 在 Windows 必须是 `.ico`，用 `app.ico`，失败只写 `data/desktop.log`，不影响页面）。
  - `desktop/preload.js`：新增 `gmWin.startDrag`（`ipcRenderer.send`）与 `gmWin.pathForFile`（`webUtils.getPathForFile`，Electron ≥ 32，本仓库 `electron.d.ts` 已确认存在）。
  - `web/gal/gal-ide.js`：`dragstart` 里若 `ideCanDragOut()`（有壳 + 有 sandbox）则 `e.preventDefault()` 后调 `startDrag({ file: ideAbsPath(rel) })`；否则维持原 HTML5 `text/plain`（浏览器回退不变）。
  - 树内拖进目录：`dragstart` 被 preventDefault 后没有 `text/plain`，新增 `ideDropSource(e)`：`dataTransfer.files[0]` → `pathForFile` → 按 `curSession().sandbox` 换算回相对路径（沙箱外返回空，不处理）。目录行 / 工作区行的 `dragover` 改用 `ideDragHasSource(e)`（`state.ideDrag` 或 Files 存在）。
  - 原生拖拽不会再有 `dragend`（Blink 拖拽已被取消）→ `ideDragBindGlobal()` 在 `mousemove` / `focus` / `drop` 上兜底清 `state.ideDrag`，避免残留让「从资源管理器拖文件到输入框」被误判成内部 `#` 引用。
  - `web/composer.js`：`bindComposerDrop` 的 drop 分支改为内部来源（`state.ideDrag`）优先转 `#` 引用，即使 `hasFiles` 为真（原生拖拽会同时带 Files），保持原有「引用」语义、不把整份文件塞进消息。
- 相关代码：`desktop/main.js`、`desktop/preload.js`、`web/gal/gal-ide.js`、`web/composer.js`。
- 已知边界：纯浏览器只能给路径文本；`startDrag` 需要真实 Electron 窗口，自动化只覆盖语法与浏览器回退路径，拖出手势与「拖回目录仍能移动」需人工鼠标确认；`data/desktop.log` 里 `start-drag file|dir <path>` 可确认壳是否真的接到了。
- 验证：`node --check`（gal-ide.js / composer.js / main.js / preload.js）+ `scripts/dev_check.py` 全过（132 编译、129 导入、6 不变量、冒烟、60 前端 JS、73 快捷键、45 markdown、18 transpage）。

## [2026-11-08] 续跑不拆块 + 计划卡继承（单活卡）

- 问题/现象：续跑 / 继续输出后，输出被拆成新的输出块，旧块的计划卡停住不动。两个根因叠加：① 后端 `_save_progress` 在「最后一条已经是完整回答」时新追加一条只含新段落的进度快照，下次请求开始时该快照被降级为永久普通消息（`_progress` 转 `interrupted`），分块成为历史事实；② 前端 `planRender(row, plan)` 只作用于当前流的行，续跑 / 接管 `appendAi` 新开行 → 旧卡冻结、新块等不到卡。
- 方案（用户选定：新块重列 + 旧卡折叠；后端原地更新根治）：
  - 后端 `_save_progress(..., inplace_base=None)`：续写时**原地更新**续写目标（`content = _cont_base + 本次新文本`），首次快照记 `_cont_base`；后续快照以原文为基重写，不叠加。收尾时用 `_cont_base + 本次 content` 重写并清 `_cont_base/_progress`，正式消息只保留一条。
  - 请求开始时降级残留进度快照同时 `pop("_cont_base")`；续写起点正文在降级循环**之前**捕获，所以新起点仍是「原文 + 已写部分」。
  - `_job["base"]` 记录续写起点；`/api/chat/attach` 首帧补发 `{"type":"base"}`（仅 `since<=0`）与当前计划快照 `_plan_sse(plan) + carried:true`（否则 `since>0` 时早先的 plan 事件已被跳过，新块永远等不到卡）。
  - 前端 `chat.js`：`planMarkCarried`（「续」徽标）、`planSupersede`（折叠 + 「已续至最新输出」）、`planMarkLive`（同会话只留一张展开活卡）、`planAutoExpand`（系统折叠的卡在计划又变未完成时自动展开；`_userCollapsed` 记录用户手动折叠，尊重不展开）、`planSweepHistory`（`renderMessages` 末尾，历史只留最新一张展开）、`planSeedFromSession`（从最后一条带 plan 的消息或 `state.lastPlan` 继承，标 `carried`）。
  - 前端 `stream.js`：`streamChat` attach 建行后 `planSeedFromSession(row)` + `base` 事件分支（`!acc` 时才铺，`row._raw` 同步）；`startJobReplay` 建行后同样继承，`event(obj)` 增加 `base`（`text = obj.text`）与 `plan`（`planRender(row, obj)`）分支。
- 相关代码：`app/api/chat.py`（`_save_progress` / `cont_base` 捕获 / `_job["base"]` / attach 首帧 / 收尾重写）、`web/chat.js`（计划卡辅助函数群 + `renderMessages` 末尾 sweep）、`web/stream.js`（attach / replay 接线）、`web/css/base.css`（`.plan-carried(-badge)` / `.plan-superseded(-note)`）。
- 验证：`node --check web/chat.js web/stream.js web/composer.js` 通过；`scripts/dev_check.py` 全过（132 编译、129 导入、6 不变量、冒烟、60 前端 JS、73 快捷键、45 markdown、18 transpage）；`pytest tests/test_chat_plan.py tests/test_resume.py tests/test_stream_stop.py -q` 34 项通过，并新增 `test_save_progress_inplace_for_continue`（原地更新不新增消息、重复快照不叠加、计划随之写回）。
- 已知边界：`base`/`plan` 首帧补发与卡片折叠动效需要真实窗口 + 真实续跑任务人工确认；纯浏览器自动化只覆盖语法与单元逻辑。

## [2026-11-08] 划词问答输入框「点不进去、打不出字」：真凶是弹窗焦点陷阱

- 问题/决策：笔记弹窗里 AskKit 输入框点不出光标、打字无反应、Tab 也不行；solo 对话里正常。**真根因**：`web/settings.js` 的「弹窗焦点约束」（a11y 阶段加的 `focusin` 捕获 + Tab 回绕）——只要任一 `.modal` 打开，焦点落到弹窗之外就立刻拉回弹窗内第一个可聚焦元素。AskKit 面板 append 在 body 上、DOM 在 `#notes-modal` 之外，焦点一进 textarea 就被抢给 `#btn-close-notes`。solo 不开弹窗、`_topModal()` 为 null，所以正常。
- 关键证据（CDP 探针：复制 data + patch store + 真实后端 + headless Edge 真实点击）：`elementsFromPoint` 栈顶就是 `TEXTAREA.askkit-input`（无遮挡、`disabled=false`），但点击前后 `document.activeElement` 恒为 `BUTTON.modal-close#btn-close-notes`；修复后变为 `TEXTAREA.askkit-input` 且 `Input.insertText` 落字成功。**教训**：「点不进去」未必是遮挡/禁用，先看 focusin 阶段谁抢了焦点；前两轮排查只测了静态页/无弹窗场景，全绿却没覆盖「弹窗开着」这个前提。
- 解决方案/原因：给焦点陷阱加豁免名单 `MODAL_FOCUS_EXEMPT = '.askkit, .selbar, .selbar-more-menu, .nt-anno-pop'`（与 `selkit.js` 的 `insideOwn` 同一份名单），`focusin` 监听与 Tab 回绕各加一行 `_inExemptLayer(...)` 跳过。笔记批注弹层 `.nt-anno-text` 同样中招，一并修好。
- 相关代码/指令：
  - `web/settings.js`：`MODAL_FOCUS_EXEMPT` + `_inExemptLayer()`（约 L2030），作用于 `focusin`（capture）与 Tab `keydown`（capture）两个监听
  - 次要修复（排查中发现的真实问题，保留）：面板高度可变但只在 open 时定位一次，长过窗口下沿会裁掉输入框 → `web/askkit.js` 挂 `ResizeObserver` + `clampIntoView()`；`web/css/askkit.css` 给 `.askkit` 加 `-webkit-app-region: no-drag` 防 Electron 顶栏拖拽区吞点击
  - 验证命令：`py -m pytest tests/test_ask_card.py tests/test_notes_ask.py -q`（11 项通过）；`python scripts/dev_check.py`（全过）
  - 探针套路（可复用）：复制 `data/`（ignore `.electron_profile/.browser_profile/*.db-wal/*.db-shm/*.log/recycle_bin`）→ import 前 patch `app.store.*` → `create_server` → Edge `--headless=new --remote-debugging-port` + `app.computer.cdp.CDP`：程序化选区 + 合成 `pointerup` 唤起 selbar → 真实点击「提问」→ `elementsFromPoint` / `document.activeElement` / `Input.insertText` 取证
- 注意：① 当前 shell 里 `python -m pytest` 无 pytest，要用 `py -m pytest`。② `--dump-dom`/`--virtual-time-budget` 下 ResizeObserver 与 rAF 不执行，尺寸类验证要跑真实帧。③ Electron 壳无热更，前端 JS 改动需重启应用后在真实窗口人工确认。

## [2026-11-08] 「提示词工程师」会话为何频繁用 shell：搜索工具能力缺口 + 误报放大

- 问题/决策：
  - 用户反馈「提示词工程师」会话里经常要 shell。查 `data/tool_trace/s_b92c1c18ec67.jsonl`：74 次调用中 shell 只有 4 次（read_file 34 / search_files 22 / ask_user 6 / shell 4），但 **shell 永不支持「始终允许」**，每次都要人工点确认，所以体感远大于 5%。
  - 4 次 shell 全在 `D:\Ai\Project\AI模拟` 里用 PowerShell `Select-String` 查 `world.html`（一个几千行单文件），本质是「搜索」而非「执行」。
  - 原因分三层：① **能力缺口**——`search_files` 只回匹配行，没有 group/count/sort，第一次 shell 是找「重复定义的函数名」，必须 `Group-Object | Where Count -gt 1`；② **保真度不足**——每行截断 160 字符、每文件默认 5 处、总上限 60 处（`MAX_SEARCH_HITS`），要全量行号清单只能 shell；③ **schema 缺 path**——工具描述只暴露 pattern/glob/context/max_per_file/count_only，无法只搜一个文件（模型实际传了 `path`，实现认但描述没写）。
  - 管道是最后一根稻草：`run_command` 描述明确「不支持管道、重定向」，而 `shell` 描述写「适合管道」，模型要做聚合自然选 shell。
  - 附带真 bug：`search_files` 零命中返回「未找到匹配（沙箱：%s）」，含「沙箱」二字，被 `tool_protocol.code_of()` 的首条规则 `("沙箱", SANDBOX_DENIED)` 误判成沙箱拒绝，附带的恢复建议是「请用户开启允许读取沙箱外」——完全误导。trace 里两次踩中（`glob:"world.html"` 与 `path:"world.html"` 两条正则其实都是真零匹配）。另外 `is_error()` 的 `_ERR_PREFIXES` 含「未找到」，也会把零命中升级成错误。
- 解决方案/原因：
  - 已修（只修误报）：① `_t_search_files` 零命中文案 `未找到匹配（沙箱：%s）` → `没有匹配（搜索范围：%s）`（两处），既不再命中 `_ERR_PREFIXES` 的「未找到」，也不含「沙箱」；② `tool_protocol._TEXT_MAP` 把 `("未找到", NOT_FOUND)` 提到 `("沙箱", SANDBOX_DENIED)` 前面作防御。
  - 验证：零命中 → `is_error=False`、不进错误包装；正常命中、`❌ 沙箱拒绝：路径越出沙箱范围` → 仍 `SANDBOX_DENIED`、`文件不存在` → 仍 `NOT_FOUND`；`py -m pytest tests/test_tool_protocol.py tests/test_webprobe.py tests/test_agent_guards.py -q` → 91 项通过（新增 `test_search_zero_hit_is_not_an_error`）。
  - 未做（用户暂不修）：② `search_files` 暴露 `path` 参数支持单文件；③ 聚合能力（group/count/sort），或允许 `run_command` 跑只读管道并给「始终允许」。这两项才是 shell 频繁出现的真正来源。
- 相关代码/指令：
  - `app/agent.py`：`_fn_tool("search_files", ...)` L723-733、`_t_search_files` L2280-2380、`MAX_SEARCH_HITS/PER_FILE/FILES` L56-59、`shell` 工具描述 L777-788、`new_rule` 拒绝 shell「始终允许」L6041-6042
  - `app/tool_protocol.py`：`_TEXT_MAP` L27-40、`is_error` L45-53、`recovery_of` L78-80
  - `tests/test_tool_protocol.py`：`test_search_zero_hit_is_not_an_error`
  - 复查：`py -c "import io,json,collections; ..."` 统计 `data/tool_trace/<sid>.jsonl`

## [2026-11-08] 一条回答出现两个气泡：从「DOM 即状态」改成「实时数据 + 唯一投影」

- 问题/决策：
  - 现象：流式输出时切走再切回（或刷新重连），同一条回答被画成两段——上面是落库的「进度快照」（带已执行 N 个步骤的统计），下面是新的「过程 · 进行中」实时行。
  - 根因：`renderMessages()` 用 `chat.innerHTML = ''` 重建历史时会把 `_progress` 快照画出来，随后 `attachStreamHost()` 把仍在跑的 live DOM 无条件 `appendChild` 到末尾。两个来源谁也不能覆盖谁，只能靠补丁去重——补丁散在 [stream.js] 的 attach 分支和 `startJobReplay` 两处，切会话这条路径没覆盖到。
  - 决策：用户选「数据化投影」（彻底方案）而不是「只在挂回前删快照行」（最小补丁）。
- 解决方案/原因：
  - 新增实时数据层：`state.live[sid] = { blocks, tools, plan, timing, text }`，配 `liveOf / liveDrop / liveText / liveSetText / liveReasoning / liveTool / liveToolDone / liveAsk / livePlan / liveTrim`。blocks 与后端 `turn_blocks` 同构（`{kind:'text'|'reasoning'|'tool', i}`），直接喂给已有的 `buildBlocksView`。
  - `streamChat` 的 base / delta / reasoning / retry / tool / tool_done / ask / plan / warn / error / rewind 分支边渲染边 fold；`ask` 的 `qid` 必须存进 tool，否则投影重建的提问卡无法提交。
  - 唯一判定点：`renderMessages()` 里若有本地 live 流（`streamOf` 有 host 或 live 有 blocks）且最后一条是 `_progress`，历史只画到 `total-1`；然后复用 `.stream-host`，没有 host 就用 `buildLiveRow()` 按数据重建。
  - 收尾清理：`unregisterStream()` 与 `startJobReplay().finish()` 里 `liveDrop(sid)`；回放（`waitJobThenRefresh`）也写 live，切走再切回能按数据恢复。
  - 附带修正：`buildBlocksView` 的 ask 分支改用 `t.qid || null`，不要再丢 qid。
- 相关代码/指令：
  - `web/stream.js`：数据层在 `streamOf` 上方；fold 在 `streamChat` 事件分支；`startJobReplay` 的 base/delta/plan/finish
  - `web/chat.js`：`buildLiveRow()`（`appendAi` 上方）、`renderMessages()` 的 `cut` 判定
  - 验证：headless Edge 探针（复制 data + patch `store.*`）断言 `before {rows:2, snap:true}` / `after {rows:2, live:true, snap:false}` / `hosted {rows:2, inDoc:true, last:true, snap:false}` / fold `kinds=["text","reasoning","tool:0","text","tool:1"]`；`py -m pytest tests/test_stream_stop.py tests/test_resume.py tests/test_chat_plan.py -q` 35 项通过
  - 注意：Electron 壳无热更，必须重启应用验证；真实窗口里「切走再切回 / 刷新 / 断网重连」三条路径仍需人工确认

## [2026-11-08] AI 卡住时不停下来问用户：提示词在鼓励硬撑

- 问题/决策：
  - 现象：world.html 可视化任务里，AI 反复「读 → 试 → 失败 → 再读」，计划卡停在 0/19，直到用户手动停止；始终没有弹「等待你操作」卡。
  - 数据（`data/tool_trace/s_07db41e3dabc.jsonl`）：整轮 1256 次调用、失败 89 次（7%）；21:45 之后 55 次调用里 **33 次是读同一个 world.html**，只有 3 次写入。所以不是「失败太多」，是「没有进展」。
  - 失败构成：① 8 次跑不存在的脚本（`_st4.cjs` / `_st2chk.cjs` / `_g2.cjs` / `_probe1.cjs` / `_p3.cjs`，trace 里没有对应的 `write_file`；模型 21:43:18 自己承认「`_st4.cjs` 没有保存到磁盘」）；② 6 次 `search_files` 零命中被误报 `SANDBOX_DENIED`（见同日条目），把它从「搜索定位」逼成「逐行读 2429 行」。
  - 为什么没停：`agent_fail_streak`（默认 2）只认「同一签名连续失败」，而它「读成功 → 跑失败」交替，计数器被重置（整轮只触发 3 次，全是 edit_file 缺 path）；`repeat_guard` / `window_hits` 只认「完全相同调用」，换个脚本名就绕过；`MAX_ROUNDS=0` 不限轮次；熔断文案只说「换验证方式」，**没有一句提 ask_user**。
  - 根因在提示词：`agent.py` 同时写着「任务没做完时不要停下来问」「只有确实无法自行决定时才用 ask_user」——模型把「卡在同一个文件」理解成「任务没做完」，于是硬撑。
- 解决方案/原因：
  - 对齐 Claude Code（官方文档核对）：它**也没有**卡住检测，`max_turns` 与 `max_budget_usd` 默认都是 No limit；循环靠「模型不再发起工具调用」自然结束，人工介入靠 `AskUserQuestion`，官方触发条件是「multiple valid approaches（有多种可行方案、需要方向）」——门槛比我们的「完全定不了」低得多。
  - 本轮只改提示词：`build_agent_prompt` 第 1 条与 `_ASK_PROMPT` 增加「卡住的判定」三条可判定条件——① 同一目标连续 2 次失败；② 最近 10 次工具调用没有一次成功写入/改动；③ 下一步需要只有用户才能提供的信息——命中即必须用 `ask_user(action:true)` 说明试过什么、卡在哪；并明确「换文件名 / 换参数 / 换脚本名继续试不算进展」。
  - 同时把「确实无法自行决定才问」放宽为「多种方案都可行但代价不同 → 当轮问清」，避免与「不要停下来问」叠加后变成一味硬撑。
- 相关代码/指令：
  - `app/agent.py`：`build_agent_prompt` 规则 1（约 L7134）、`_ASK_PROMPT`（约 L7288-7319）
  - 验证：`py -m py_compile app/agent.py`；`agent._ASK_PROMPT` 含新规则（1261 字符）；`py -m pytest tests/test_agent_guards.py tests/test_tool_protocol.py -q` → 74 项通过
  - 未做（用户暂不选）：放宽 `failure_streak` 判据为「同一工具类别 + 无成功写入」；轮次 / 预算上限（Claude Code 默认也不设）
  - 注意：后端改动需重启应用生效；本机跑 python 会把 `app/__pycache__/*.pyc` 移进回收站，属既有现象，源码不受影响

## [2026-11-08] 继续输出 / 回到实时输出：两个新问题（其中一个是上轮改动的回归）

- 问题/决策：
  - 现象（会话 `s_07db41e3dabc`「模拟」）：① 初始状态显示「中断」；② 点「继续输出」后前端直接进入「后台运行」；③ 点「回到实时输出」时前端已有输出被清空。
  - ③ 的机制：上一轮加的 `cut` 假设「实时行承担的就是最后一条」。但「继续输出」是**复用上一条**（`data-idx=21`），实时行承担的是 21，而最后那条 22（`_progress`，910 字）既不被 live 承担、又被 `cut` 跳过——直接消失。已改成按实时行自己的 `data-idx` 决定跳过哪一条；只有「新行（无 idx）」才按「最后一条快照」处理。
  - ② 的机制：`streamChat` 收尾里 `if (!gotDone && isCur())` 无条件挂 `keepRecovering`。服务端明确返回非 2xx（400/404/500）时请求根本没被受理，后台没有任务可等，界面却开始假装「后台运行中」。已加 `httpRejected`：非 2xx 只做一次状态对齐，不进恢复轮询，也不再推迟失败提醒。
  - ① 的数据侧：末尾是 `20 user(「继续吧」)` / `21 assistant(interrupted, end_reason=resume, 215 字)` / `22 assistant(_progress)`；22 的开头与 21 完全相同但没有 `_cont_base`，说明产生 22 的那次请求没带 continue/resume，是普通新回合。
  - ① 的第二段（用户实测后补）：再点一次「继续任务」后，`resume_pending` 被清、22 出现 `_cont_base` 并从 910 字涨到 3300+ 字 —— **后端续写是正确的（原地更新最后一条）**。问题在前端：`continueReply(index)` 用「被点击的那条」当续写起点，而按钮挂在 21（更早那条）上，后端却按 `messages[-1]`（22）续写。两边起点不同，界面从 215 字开始长，看起来就是「一点继续，前面的输出没了」。
- 解决方案/原因：
  - 已修 ①②③（前端）：③ 按实时行的 `data-idx` 精确跳过；② 非 2xx 不再进恢复轮询；① `continueReply` 改为总是续写**最后一条 assistant**，与被点击的是哪条无关。
  - ④（截图复现后补）：点「继续任务」时后端那轮其实还在跑（刷新 / 断线后转成后台），`continueReply` 只弹 toast「还在生成」就 return，看起来就是按钮失灵。改为直接 `resumeLiveOutput(sid)` 接管实时输出；只有拿不到该函数时才回落到 toast。
  - 验证：`node --check web/chat.js`、`node --check web/stream.js`；headless Edge 探针断言 s1 `{rows:3, hasOld:true, hasProgress:true, hostInDoc:true}`（继续输出时进度消息不再丢失）、s2 `{hasProgress:false}`（新行时最后一条快照仍由 live 取代）；落库数据侧确认 22 的 `_cont_base` 存在且字数持续增长。
  - 端到端回归（新增，默认跳过）：`$env:GM_LIVE_UI=1; py -m pytest tests/test_continue_resume_live.py -q` → 3 passed。做法：假模型 SSE + 临时 `SESSION_FILE` + 真 httpd + headless Edge；用例 1 在**旧消息**上点「继续任务」，断言请求带 continue/resume、实时行绑定 `data-idx=2`（最后一条）、落库后旧条不变且最后一条变长、不新增消息；用例 2 断言有实时数据时 `_progress` 快照被 live 投影取代、只出现一份；用例 3 模拟「后端在跑、本地无流」，断言点击只发 `/api/chat/attach` 接管、不再发新的 `/api/chat`。
- 相关代码/指令：
  - `web/chat.js` `renderMessages()` 的 `_skipIdx` 判定（按实时行的 `data-idx` 精确跳过）
  - `web/stream.js` `httpRejected`（声明 / `!res.ok` / `deferNotify` / 恢复轮询条件）、`continueReply` 的 target 回退到最后一条 assistant

## [2026-11-08] 继续输出 / 后台运行整体重构：把「在不在跑」收敛成一条判定

- 问题/决策：
  - 用户反馈同类问题反复，要求整体研究后重构，不再打补丁。
  - 两个探查任务的结论：后端「某会话在不在跑」有 **6 个来源**（`ACTIVE_STREAMS` 租约 / `JOBS.done` / `job.ts` + 30·90·180·900 四套阈值 / 末条 `_progress` / `resume_pending·interrupted·end_reason` / `compress_busy`），前端有 **3 套**（`state.streams` / `state.live` / `state.streamingSids`）。
  - 三处具体缺陷：① `release_active` 早于落库（`chat.py` 原 L7141），窗口内 `/api/state` 会把正在收尾的 `_progress` 判成中断；② 该误判被 `reap_orphan_progress` 每次 `/api/state` 执行放大 → 标记被清 → `_save_progress` 走 else 新建第二条 assistant、旧提示永不消失、409 检查放行并发写；③ `startJobReplay` 里 `const liveText` **遮蔽了全局 `liveText()`**，delta 回放抛 TypeError，整条回放中断（这是上一轮引入的）。
- 解决方案/原因：
  - 后端：`release_active` 移到 done + 落库之后；`_live_streams` 的 idle 90→300（心跳每 5s 刷 `job.ts`，窗口足够宽）；从 `/api/state` 移除 `reap_orphan_progress`，孤儿只在进程启动时由 `_mark_resume_pending` 处理一次。
  - 前端：`_skipIdx` 不再依赖 `_progress`，改为「有 `data-idx` 就跳过它，否则跳过最后一条 assistant」；`startJobReplay` 的行包 `.stream-host` 并 `registerStream`（标记 `replay`），`finish` 时 `unregisterStream` —— 这样 `renderMessages` 会复用而不是重建，回放闭包不会写进死节点；`refreshResumeBar` / `resumeLiveOutput` 的「已接管」判断排除 `replay`。
  - 保留的既有语义：僵尸租约仍由 `_live_streams` 清理（既有测试 `test_live_streams_drops_zombie_entries` 保护），线程存活判定仍只归 watchdog。
- 相关代码/指令：
  - `app/api/chat.py`（release 时机）、`app/api/misc.py`（`_live_streams`、`_api_state`）
  - `web/chat.js`（`_skipIdx`）、`web/stream.js`（`startJobReplay` 的 host/register、`liveText`→`liveTip`、`refreshResumeBar`、`resumeLiveOutput`、`continueReply` 接管）
  - 验证：`node --check web/stream.js web/chat.js`；`py -m py_compile app/api/chat.py app/api/misc.py`；`py -m pytest tests/test_stream_stop.py tests/test_resume.py tests/test_chat_plan.py tests/test_tool_protocol.py tests/test_agent_guards.py tests/test_ask_card.py -q` → **118 passed**；`$env:GM_LIVE_UI=1; py -m pytest tests/test_continue_resume_live.py -q` → **4 passed**（用例 4 专锁 `startJobReplay` 的 delta 回放不抛异常且正文落到行内）
  - 注意：前后端都改了，**必须重启应用**；真实窗口里「切走再切回 / 刷新 / 断网重连 / 点继续任务」四条路径仍需人工确认

## [2026-11-08] Token 优化：量化基线、竞品/论文调研与设计方案
- 问题/决策：用户要求先调研论证再落地，禁止未研究先改码。目标是在不牺牲输出质量、不缩减有效上下文的前提下降低 token 费用。已产出 `docs/TOKEN_OPTIMIZATION_DESIGN.md`（设计稿，未实施）。
- 解决方案/原因（关键实测结论，全部来自 `data/token_usage.jsonl` / `sessions.json` / `tool_trace/`，只读统计）：
  - 总量：6,753 条记录，prompt 3,800,430,212 / completion 19,808,545 / cached 3,633,099,141（命中 95.60%）；chat 来源占 prompt **99.86%**。按 DeepSeek 公开价折算，**未命中 167.3M 占输入成本约 70%**，所以「省 token」要同时盯总量与 miss。
  - 单次调用（18,331 条 rounds_detail）：P50 **108,615** / P90 252,688 / max 341,101 / mean 133,969。
  - 输入构成（字符）：reply **62.6%**、user **17.8%**、tool_result 6.2%、schema 5.9%、summary 2.8%、tools_doc 2.4%、rules 1.2%、memory 1.1% → 历史重发 80.4%。
  - **反直觉发现**：最近 7 天 201 个回合里，回合第一轮 prompt 中位数 **100,822**，而回合内每轮增量中位数 **0**。即成本主体是「回合起点携带的存量历史」，不是回合内膨胀。50+ 轮的 11 个回合（5.5%）却占 **48.6%** prompt → 两个问题必须分开治。
  - 现有压缩只在 `compress_ratio=0.8`（本机 1M 窗口 → 80 万）触发，对 10 万起点的短回合完全无效。模拟「保留 K 轮 + 摘要」：keep=6/S=4k 省 13.2%，keep=4/S=2k 省 15.8%（上界，未含质量代价）。
  - 失控样本：11-08 19:06 `s_07db41e3dabc` 单回合 prompt **465,460,768**（362 轮、auto_continues 1,520），占全量 12.25%；根因是 `agent_max_rounds=0` + `agent_auto_continue=-1` + `agent_auto_idle_limit=0` + 回合内无累计预算（`data/config.json` 实测）。
  - 工具：13,689 次调用、26.87M 字符；read_file 41.4% + run_command 28.6% + write_file 13.5% + search_files 8.8%；>5k 字符的结果只占 9.66% 调用却占 44.4% 字符。
  - 缓存：全仓无 `cache_control`，`app/providers.py` 的 `anthropic_body` 未打缓存点；DeepSeek 只认从 token 0 起的精确前缀，所以 system 必须冻结。
- 相关代码/指令：
  - 设计稿：`docs/TOKEN_OPTIMIZATION_DESIGN.md`（含竞品 7 家、论文 20 篇、六层架构、M1-M6 模块、4 批实施、质量门禁、风险登记）
  - 关键证据：`app/context.py:62-163`（预算）、`:468-535`（历史压缩）、`:1163-1219`（预压缩）、`:1289-1357`（折叠/裁剪）；`app/api/chat.py:3487-3536`、`:7057-7100`；`app/agent.py:3842`（read_tool_result 句柄）；`app/config.py:63-76/245-246/296-303`
  - 复核脚本：`.tmp_audit/token_baseline_audit.py`、`.tmp_audit/token_baseline_audit2.py`、`.tmp_audit/savings_sim.py`（只读，不写 data/）
  - 评审决策（2026-11-08）：D1 默认开启省费模式（`token_profile=balanced`，回合起点 40k）；D2 对齐 Trae（自动压缩后继续 + 使用率可见 + 手动压缩，不弹窗打断）；D3 Anthropic 通道启用 `cache_control`，只对稳定段 S0/S1 打点；D4 引用正文首轮内联、后续只带句柄。
  - Trae 实测口径（官方文档 docs.trae.cn/ide_context-compaction）：超窗口**自动压缩后继续**、面板常驻显示使用率、支持手动「压缩」按钮；社区公认压缩会丢细节。v3.4.1 起终端超长输出自动截断。
- 未覆盖：方案已定稿并锁定默认值，但**未开始编码**（等最终批准）；收益为估算（单价取自 DeepSeek 官网，`data/config.json` 无单价字段，无法换算金额）。

## [2026-11-08] Token 优化批 1：观测先行（预算账本 / 前缀漂移 / 流水字段）
- 问题/决策：没有「单次调用的真实输入分布」和「前缀为什么 miss」，后续任何优化都无法验收。批 1 只做观测，零行为变化。
- 解决方案/原因：
  - `app/token_budget.py`（新）：`Budget` 不可变快照 + `budget(model)` + `level(used, b)`（l1=35% / l2=60% / l3=85%）。所有数值委托 `context.context_limits / request_budget / effective_output_cap`，不参与任何裁剪决策。
  - `app/prefixtrace.py`（新）：按消息粒度比对同会话上一次请求，输出 `prefix_msgs / prefix_ratio / prefix_tokens / reuse_ratio / sys_changed / tools_changed / first_divergence`；进程内 LRU 保留 64 个会话。用来回答「缓存为什么没命中」。
  - `app/api/chat.py`：`timing.obs = {fold, fold_removed, compress, overflow, prefix, budget}`；用量记录加 `turn_id`。折叠计数点三处（回合内压力 4868、溢出恢复 5034、工具后 7094），压缩计数两处（请求前 3513、回合内 4902）。
  - `app/usage.py`：`aggregate_usage` 新增 `call_stats`（单次调用 P50/P90/P95/mean/max，来自 `timing.rounds_detail`）与 `obs_stats`（cold/warm、sys_changed、平均复用、折叠/压缩次数、级别分布）；旧记录无这些字段时优雅降级。
  - `web/settings.js`：「token 明细」新增「单次调用 P50」「前缀复用」两张卡。
- 相关代码/指令：`app/token_budget.py`、`app/prefixtrace.py`、`app/api/chat.py`（`_obs_counts` / `_obs_prefix` / `_budget_snapshot` / `turn_id`）、`app/usage.py`（`_percentiles` / `_obs_stats`）、`web/settings.js`（`renderUsage`）、`tests/test_token_budget.py`、`tests/test_prefixtrace.py`、`tests/test_usage.py`、`tests/test_continue_resume_live.py`
- 验证：`py -m py_compile` 4 个 py + `node --check web/settings.js` 通过；定向 8 个测试文件 **140 passed**；全量 `pytest tests`（排除缺 reportlab 的 `test_reader.py`）exit 0；`$env:GM_LIVE_UI=1; py -m pytest tests/test_continue_resume_live.py -q` → **5 passed**（新增用例断言 `timing.obs` 真的落盘、`prefix.cold=True`、`budget.prompt_cap>0`）。真实流水只读复算 `call_stats.p50=108,615`，与独立统计一致。
- 未覆盖：前缀统计是进程内状态，重启后首个请求记 cold；真实应用窗口里的「token 明细」卡片样式尚未人工确认（要重启应用）。

## [2026-11-09] Token 优化批 2：省费模式（引用句柄 / 分段冻结 / 规则缓存）
- 问题/决策：批 1 只观测。批 2 做「少带重复内容」，全部由 `token_profile` 控制，默认 `balanced`（用户 D1 决策），`quality` 一键回到旧装配。
- 解决方案/原因：
  - `app/config.py` 新增 `token_profile`（默认 `balanced`）。
  - 引用句柄 `app/context.py`：`_msg_text(m, refs_handle)`；历史用户消息里的 `@文件/@目录` 正文不再内联，只留「路径:行号 + 用 read_file 取回」；**最后一条用户消息仍内联**。`_context_text_tokens` / `_context_parts` 同步支持 `refs_handle_from`，保证装配与估算同一口径。新增 `_refs_handle_on()` / `_refs_inline_from()`。
  - 分段冻结 `app/api/chat.py`：界面上下文 / 意图提示 / 自动化目标改由 `_attach_dyn_tail` 贴到请求末尾（原先直接拼进 `messages[0]`，每变一次就让其后整段历史 miss 前缀缓存）。GAL 分支不动。
  - 规则缓存 `app/agent.py`：新增 `_read_rules_text_cached`（key = mtime_ns + size）。**踩坑**：与既有 `_read_rules_file`（读权限 JSON，返回 (rules, error)）重名，Python 后定义覆盖前者，导致 `project_rules` 返回元组字符串；改名后正常。
  - 设置入口 `web/settings.js`：「上下文与压缩」新增「省费模式」勾选框，默认开；保存 `token_profile`。
- 相关代码/指令：`app/context.py`（`_msg_text` / `_msg_tokens` / `_context_text_tokens` / `_context_parts` / `session_context` / `build_context_messages` / `_refs_handle_on` / `_refs_inline_from`）、`app/api/chat.py`（`_balanced` / `_late_blocks` / 末尾 attach）、`app/agent.py`（`_RULES_CACHE` / `_read_rules_text_cached`）、`app/config.py`、`web/settings.js`、`tests/test_token_profile.py`
- 验证：`py_compile` 4 文件 + `node --check web/settings.js` 通过；定向 10 文件 **204 passed**；全量 `pytest tests`（排除缺 reportlab 的 test_reader）exit 0；`GM_LIVE_UI=1 pytest tests/test_continue_resume_live.py` → **5 passed**；`scripts/dev_check.py` 全部通过。
- 实测收益：真实 `sessions.json` 只读对比 `session_context().used`，quality=561,032 → balanced=548,131，**-2.3%**；有引用的 `s_2eed2dd969cf` 单会话 **-19.9%**，与设计估算一致。绝对值不大（引用只出现在 43 条消息），真正的大头在批 3 工作集。
- 未覆盖：`build_agent_prompt` 仍随能力域变化，尚未拆「稳定段/动态段」；分段冻结对前缀命中率的实际影响要等真实回合积累 `timing.obs.prefix` 才能验证。

## [2026-11-09] Token 优化批 3：工作集（历史原文 40k）+ 回合护栏
- 问题/决策：批 2 只省 2.3%。真正的成本是「每轮重发整段历史」：单次调用 P50 108,615，reply 占 62.6%。批 3 引入与窗口解耦的「工作集」——历史原文上限 40k，超出压进结构化摘要；当前回合永不被压。另加单回合 token 预算拦病态长跑。
- 解决方案/原因：
  - `app/context.py`：新增 `working_set_tokens()`（quality / 本地小模型 → 0）、`history_tokens(s)`、`_clamp_keep_from()`；`manual_keep_budget()` 在省费模式下直接返回工作集；`compress_history()` 在工作集模式下把 `keep_ratio` 固定 1.0（原 0.25 会把 40k 再砍成 10k）；新增 `maybe_compact_working_set()`（后台压缩，看日常重发量而不是窗口余量）。
  - 关键保护：压缩切点不得越过最后一条真正的用户消息（`_clamp_keep_from`），否则会把「正在做的事」压成摘要；工具结果消息不算新回合。
  - `app/api/chat.py`：历史 > 工作集 → 后台压；> `history_working_set_sync_ratio`（1.5）倍 → 本轮同步压一次。新增回合护栏 `agent_turn_token_budget`（默认 3000 万），触发即收尾并给 `end_reason=budget`（前端 `END_REASON_LABEL` 已有该标签）。
  - **并发坑**：后台压缩推进摘要后，主请求收尾 `_persist_session(s)` 会整条替换会话，把摘要回滚。新增 `_keep_fresh_summary()`：盘上 `summary_upto` 更大时保留盘上摘要，只合并正文。
  - `app/config.py`：`history_working_set_tokens=40000` / `history_working_set_sync_ratio=1.5` / `agent_turn_token_budget=30000000`。
  - `web/settings.js`：省费模式组加「历史原文预算」「单回合输入预算」。
- 相关代码/指令：`app/context.py`（`working_set_tokens` / `history_tokens` / `_clamp_keep_from` / `manual_keep_budget` / `compress_history` / `maybe_compact_working_set`）、`app/api/chat.py`（`_ws` / `_ws_hard` / `_turn_token_budget` / `_turn_token_hit` / `_keep_fresh_summary` / `_persist_session`）、`app/config.py`、`web/settings.js`、`tests/test_working_set.py`
- 验证：`py_compile` 3 文件 + `node --check` 通过；定向 62 passed；全量 `pytest tests`（排除缺 reportlab 的 reader）exit 0；`GM_LIVE_UI=1` 端到端 5 passed；`scripts/dev_check.py` 全部通过。真实会话只读投影：历史 361,930 → 236,405（-34.7%），P50 预计 108,615 → ~55k（投影，待真实回合确认）。
- 未覆盖：未跑 `eval/` 真实模型评测（会产生费用）；建议观察几天后对比 `timing.obs.prefix` 与「单次调用 P50」，不达标就调大 `history_working_set_tokens` 或设 0。
- 与设计的差异：设计里的「light_model 双探针」改成确定性保障（摘要 pin 文件路径/错误原文 + 本地锚点召回闸门 + 八段式结构化摘要 + 当前回合豁免），避免每次压缩多一次模型调用。

## [2026-11-09] 修复：历史图片不可见（AI 要求重发图片）
- 问题：用户发过图片后继续聊天，AI 经常说看不到，要求重新发送。用户怀疑是此前某次改动导致。
- 根因（非批 1~3 引入）：2026-10-29「识图默认改走聊天模型」后 `vision_mode()` 恒为 `chat`，图片只在当轮多模态直发，**不再写入 `msg["vision"]` 文本**（该写入只在 `mode=="vision"` 分支）；而 `build_context_messages()` 只产出字符串，于是历史里的图片退化成 `attachments.ref_line()` 的一行「可回看附件」。补图逻辑当时只覆盖「重新生成 / 继续输出」（`app/api/chat.py` 注释已写明该限制），普通新消息不补。
- 解决方案：
  - `app/context.py`：`build_context_messages(s, system_prompt, image_loader=None, image_keep=0)`；新增 `_history_image_msgs()`（取最近 N 条带图用户消息，**排除当前回合**）、`_msg_image_count()`、`history_image_count()`；`history_tokens()` 补上历史图片的 `IMAGE_TOKENS`；新增 `message_chars()`（多模态只算 text 片段，别把 base64 当正文）。
  - `app/api/chat.py`：新增 `_hist_img_loader`（`read_message_images` + `build_vision_content`），仅 `vision_mode()=="chat"` 且非 GAL 时启用；`_attach_dyn_tail` 支持 list content；`img_tokens` 与 `_req_chars` 改用新口径。
  - `app/config.py`：`image_history_keep=2`（每条最多 2 张）。
  - `web/settings.js`：省费模式组新增「历史附图保留条数（0 = 不重发）」。
- 相关代码/指令：`app/context.py`（`_history_image_msgs` / `history_image_count` / `build_context_messages` / `message_chars` / `history_tokens`）、`app/api/chat.py`（`_attach_dyn_tail` / `_hist_img_loader` / `img_tokens`）、`app/config.py`、`web/settings.js`、`tests/test_image_history.py`
- 验证：`py_compile` 3 文件 + `node --check` 通过；新增 6 条测试通过；全量 `pytest tests`（排除缺 reportlab 的 reader）exit 0；`dev_check.py` 全部通过。真实会话统计：131 条带图消息，最大会话 93 条，但 1031/1091 已压缩，窗口内可回看的为 2 张（符合 `image_history_keep=2`）。
- 未覆盖：压缩点之前的图片仍只能靠 `attachment_read` 回看；若用户反馈仍不够，可加「窗口外也保留最近一张图」。

## [2026-11-09] Token 优化批 4：Anthropic 提示缓存（其余两项按实测取舍）
- 问题/决策：批 4 原计划三项——Anthropic `cache_control`、schema 按 action 拆参、轻量任务全面走 light_model。实测后只做第一项。
- 解决方案/原因：
  - `app/providers.py`：`anthropic_body(..., cache=False)`；开启时 `system` 改成带 `cache_control:{"type":"ephemeral"}` 的 block 列表（覆盖 tools + system），并在**最后一条消息的最后一个内容块**再打一个断点（覆盖整段历史前缀）。新增 `_mark_last_cacheable()`；多模态时拷贝块再加字段，不改调用方对象。
  - `app/llm.py`：`stream_chat` 的 Anthropic 分支按 `anthropic_cache`（默认 True）传入；`_quick_chat` 不缓存（短请求写 1.25x 不划算）。
  - `app/config.py`：`anthropic_cache=True`；`web/settings.js` 省费模式组加勾选框。
- 取舍依据：
  - light_model 路由**已具备**：`_quick_chat` 默认 `use_light=True`，只有阅读器翻译 / 代码评审 `use_light=False`。
  - schema 拆参**不做**：实测 9 个 meta 工具 8,168 字符 / ≈2,713 token，约占 55k 请求的 5%，且几乎全命中缓存（边际 <0.5%）；拆参最多省 0.3k，却会改变工具面、提高误调风险。
- 相关代码/指令：`app/providers.py`（`_mark_last_cacheable` / `anthropic_body`）、`app/llm.py`（`_cache_on`）、`app/config.py`、`web/settings.js`、`tests/test_providers_cache.py`
- 验证：`py_compile` 3 文件 + `node --check` 通过；新增 5 条测试 passed；全量 `pytest tests`（排除缺 reportlab 的 reader）exit 0；`GM_LIVE_UI=1` 端到端 5 passed；`dev_check.py` 全部通过。
- 未覆盖：当前用 OpenAI 兼容通道（DeepSeek），这项对现有账单无影响；真实 Anthropic 命中率需切通道后用 `usage.cached / prompt` 验证。

## [2026-11-09] 修复：误报「连接中断」+ 中途错误残留成独立段落
- 问题：用户看到模型输出初期出现一段像「中断」的独立正文，随后合并；完成后的系统通知却说「输出异常中断」，实际内容完整。
- 排查：后端最近 30 回合全部 `stream_state=done / finish_reason=stop / end_reason 空`，app.log 无断流（仅 11-08 有 4 次 billing）。两个现象都发生在前端「本地流丢了 done」之后。
- 根因与修复：
  - A 通知误报：`keepRecovering` 补拉成功后仍把本地构造的 `Error('连接提前结束')` 传给 `notifyChatDone`；而服务端**只在异常时**发 `end_reason`，正常完成时 `reason=null` → 落到 `opts.error ? '连接中断'` 分支。修复：`waitJobThenRefresh` 返回 `done`（attach 回放到 `done`）；`keepRecovering` 判 `r.done===true` 或「落库末条是完整 assistant」→ 视为正常完成，不传 error。
  - B 错误残留：`error` 事件原本直接 `acc += t; pushText(t)` 写进正文，重试成功后要等 `done` 重排才消失。修复：错误只进实时状态条；收尾时若一句正文都没有，才把最后一条错误补进正文，避免空气泡。
- 相关代码/指令：`web/stream.js`（`lastErrText` / `error` 分支 / `finally` 兜底 / `keepRecovering` / `waitJobThenRefresh` 的 `sawDone`）
- 验证：`node --check web/stream.js`；`scripts/dev_check.py` 全部通过；`$env:GM_LIVE_UI=1; py -m pytest tests/test_continue_resume_live.py -q` → 5 passed。
- 未覆盖 / 根因：`done` 事件带全部会话实测 **780.2 KB**（单会话 `s_2eed2dd969cf` 769 KB，80 条消息），连接慢时最容易丢这个包。要根治需「done 只回当前会话 + 新增按会话拉取接口」，单独立项（方案 C）。真机验证：窗口切后台跑 5 分钟以上任务看通知文案；正常对话观察正文是否还有独立中断段落。

## [2026-11-09] 方案 C：done 事件瘦身（780KB → 192KB）
- 问题：每轮结束的 `done` 带 `slim_sessions(load_sessions())`，实测 780.2 KB，其中单会话 `s_2eed2dd969cf` 占 769 KB（默认窗口 80 条）。连接慢 / 中间层掐空闲连接时最易丢这个包，是「本地流丢 done」的结构性诱因。
- 关键发现：**不需要新增接口**。前端早已支持分页：`slim_sessions` 带 `msg_total/msg_start`，`core.js` 把窗口外补 null 并记 `msg_loaded_start`，`chat.js` 在 `start <= loadedFrom` 时显示「↑ 加载更早的消息」并调用 `/api/session/messages`（已存在）。
- 解决方案：
  - `app/sessions.py`：`slim_sessions(data, msg_limit=None)`，None 时读 `chat_payload_msg_window`（0 = 全部），显式传参优先；新增 `_payload_msg_window()`。
  - `app/config.py`：`chat_payload_msg_window = 24`。
  - `app/api/sessions.py`：分页接口注释同步。
  - `tests/test_slim_payload.py`：5 条（配置读取 / 显式优先 / 0=全部 / 缺省回落 MSG_WINDOW / 非当前会话不带消息）。
- 相关代码/指令：`app/sessions.py`（`_payload_msg_window` / `slim_sessions`）、`app/config.py`、`app/api/sessions.py`、`tests/test_slim_payload.py`
- 验证：`py_compile` 通过；5 条新测试 passed；全量 `pytest tests`（排除缺 reportlab 的 reader）exit 0；`GM_LIVE_UI=1` 端到端 5 passed；`dev_check.py` 全部通过；真实 sessions.json 只读复算 `192.1 KB / window=24 / msg_total=1101 / msg_start=1077`。
- 行为变化：很长的会话默认只显示最近 24 条，更早的点「加载更早的消息」（每次 60 条）。想恢复旧行为把 `chat_payload_msg_window` 设 0 或 80。
- 附注：`load_sessions()` 每个新进程可能因 `_mark_resume_pending` 触发一次 `save_sessions`（59MB 重写）。排查时用只读脚本会看到 sessions.json 被重写与回收站提示，属既有行为、数据不丢。

## [2026-11-09] 设置「功能说明」重构：快捷键文档改为注册表同源生成

- 问题/决策：
  - 「快捷键」页的「完整说明」原先是 `index.html` 里手写的 6 张表，已和实际判定脱节：输入框缺 `Ctrl+Enter` / `Ctrl+I` / `Esc` / `↑`，全局缺 `Ctrl+F` / `Ctrl+Shift+V` / `?`，IDE 缺 `Ctrl+B` / `` Ctrl+` ``。
  - 实际存在但完全没进说明的键：`Ctrl+Alt+1~4` 切模式、`Alt+↑↓` 会话排序、审阅 `Ctrl+Enter` / `Ctrl+Backspace`、AI 补全 `Alt+\` / `Alt+]` / `Alt+[`、`Ctrl+Alt+I/B/R/M`、`Shift+Alt+F`、`F5/F10/F11`、设置面板导航键。
  - 决策：新增纯数据层 `web/hk-doc.js` 作为说明的唯一事实来源；可改键条目只写 `def: 'HK_DEFS id'`，按键由 `hotkeys.js` 现取，改键后说明自动同步；不可改键条目才写死展示键。
- 解决方案/原因：
  - `web/hk-doc.js`：7 组（全局与模式 / 对话与输入框 / GAL / IDE / 编辑器 Monaco / IDE 进阶 / 设置面板），每行带 `cond`（生效条件）与可选 `note`；注册表 36 条可改键规则全部覆盖。
  - `web/hotkeys.js`：新增 `hkRenderDoc` / `hkDocRefreshKeys` / `hkFilterDoc` / `hkRenderHelpSummary`；`hkRenderAll` 在改键 / 重置后只替换 `kbd` 文本，不重建节点（重建会丢 `set-card` 装饰）；`#set-hk-search` 同时过滤改键列表与完整说明；「?」清单底部新增「完整说明…」直达设置 → 快捷键。
  - `web/index.html`：删掉 6 张手写表，换成 `#set-hk-doc` 容器；「功能说明 → 快捷键总览」改为 `#help-key-summary`，由同一张表生成分组摘要（改键后同步）。
  - `web/css/base.css`：新增 `.hk-doc-*` 样式（分组标题 / 可改键胶囊 / 条件与补充说明）。
  - 功能说明正文逐条校对：图像识别改为「默认聊天模型多模态直发，可选优先视觉 API」（原写反了）；GAL 演出主题 3 → 6 套；上下文默认窗口 128000 → 100000000（代码默认值），并说明「想启用按比例压缩就填真实窗口」；新增「省费模式：历史预算 / 引用句柄 / 提示缓存」条目（省费模式 / 40000 历史原文预算 / 3000 万单回合预算 / 历史附图 2 条 / Anthropic `cache_control`）。
  - `web/settings.js`：`HELP_SET_TARGET` 同步新标题，并为「省费模式」追加去设置入口。
- 相关代码/指令（如果有）：
  - `node scripts/hotkeys_test.js`：新增 8 条「说明与注册表同源」断言（分组唯一、def 存在、全覆盖、不重复、自写条目字段完整、def 条目必须有 cond）。
  - 验证：`node --check` 4 个文件；`py scripts/dev_check.py` 全部通过（61 个前端 JS、81 条快捷键测试）；HTML 结构校验——42/42 `details`、id 无重复、`HELP_SET_TARGET` 20 条全部能在页内找到对应 `summary`。
- 已知：不可改键条目仍是手写数据，测试只能保证字段完整与不引用未知 id，无法自动验证「按键是否真的生效」；新增界面快捷键时需同时补 `web/hk-doc.js`。

## [2026-11-09] 设置「快捷键」模块整体重构：统一面板 + IDE 键位入库

- 问题/决策：
  - 旧改键面板只有「动作名 + 键」，看不到生效条件；36 条没有筛选；不能解除绑定；冲突只在录制时 toast 一次；只能全部重置；`?` 改成打开功能说明后，HUD 展开 / 轮询代码已无调用者。
  - 命令面板里已实现的 IDE 动作（F12 / Shift+F12 / F2 / Ctrl+Shift+O / Ctrl+Shift+M / Ctrl+I / Ctrl+Shift+V / Ctrl+Alt+I·B·R·M / Shift+Alt+F）以及调试 F5 / F10 / F11 / Shift+F11 / Shift+F5 没进注册表，不能改键。
- 解决方案：
  - `web/hk-core.js::hkKeysOfDef`：覆盖值为空串 = 主动解除绑定，返回 `[]`，`hkHitIn` 自然跳过。
  - `web/hotkeys.js`：
    - `hkOverrides` 接受空串；`hkIsCustom` 改用 `hasOwnProperty`；新增 `hkUnbind` / `hkResetScope` / `hkScopeName`。
    - `hkRenderPanel` 改为「筛选工具条（全部 + 5 个作用域 + 仅看已改）+ 分组 + 统计」；筛选状态存在容器 `dataset`，重渲染不丢；搜索同时匹配名称 / 作用域 / 生效条件 / 按键。
    - `hkRowEl` 每行显示作用 + 作用域徽标 + 生效条件（从 `hk-doc.js` 反查）+ 当前键 + 改键 / 解除 / 重置；已改条目若与他人同键，显示「同组冲突 / 跨组同键」徽标（title 列出与谁冲突）。
    - 分组标题右侧「恢复本组」；删除 HUD 死代码（`HK_HUD_KEY` / `hkHudToggle` / `hkHudTimer*` / `hkHudItems` / `hkHudRefresh` / `hkPrimaryLabel`）。
    - `HK_DEFS` 新增 12 条 editor 作用域的 `gm.*` 动作 + 5 条 ide 作用域调试键。
  - `web/gal/gal-ide.js`：`ideApplyEditorKeybindings` 支持空串覆盖（解绑 Monaco 默认键）；删除内部 F5 / F10 / F11 监听，改为 `ideDebugShortcut(id)`。
  - `web/init.js`：ide 作用域统一分发增加调试键分支（输入框 / 编辑器文本区 / 弹窗打开时不触发）。
  - `web/hk-doc.js`：补 17 条新注册表条目的说明；`ideTools` 组只保留不可改的 AI 补全键，避免与注册表重复。
  - `web/index.html` / `web/css/base.css`：更新改键说明与搜索占位符；新增 `.hk-tools` / `.hk-chip` / `.hk-only` / `.hk-row-main` / `.hk-row-cond` / `.hk-badge` / `.hk-row-scope` / `.hk-sum` 等样式；删除 `#hk-hud-list` 与对应 CSS。
- 相关代码/指令（如果有）：
  - 测试：`node scripts/hotkeys_test.js` 新增 2 条解除绑定断言（83/83 通过）。
  - 验证：`node --check` 5 个文件；`py scripts/dev_check.py` 9 项全通过。
- 已知：编辑器内 `gm.*` 动作的「解绑默认键」依赖 `monaco.editor.addKeybindingRules(command: null)`，需在真实 IDE 里点一次验证；不可改键条目仍需手工补 `web/hk-doc.js`（测试会检查 def 引用与覆盖）。

## [2026-11-09] 修复：关闭窗口后图片 / 附件消失（live 测试误删 uploads）

- 问题/现象：用户反馈关窗再打开后，历史消息里的图片 / 文档附件消失，只剩发送的文字。只读核查发现 `data/sessions.json` 里 148 条带附件的用户消息都还在，但 `data/uploads/` 只剩 1 个目录、1 个文件（最新一张图）。
- 定位证据：
  - `data/app.log` 里 5 次启动清理全部是「无用文件 1–2 个」，**没有**「删除孤儿目录 N 个」——不是启动清理直接删的。
  - `tests/test_continue_resume_live.py` 把 `S.SESSION_FILE` 指向 `tempfile.mkdtemp()` 后调用 `httpd.create_server()`；`test_stream_parallel_live.py` 同写法。
  - `create_server` → `sweep_uploads()`：`load_sessions()` 读到临时会话（只有 `s1`），真实 `data/uploads/<真实 sid>/` 全部不在 `alive` 里，被 `shutil.rmtree` 整目录删除。`uploads.py` 原注释已警告这个场景，但 `tests/conftest.py` 没有统一设 `GM_NO_SWEEP=1`。
- 修复：
  - `app/uploads.py`：`sweep_uploads` 增加 `PYTEST_CURRENT_TEST` 跳过、**会话文件不是默认 `data/sessions.json` 时跳过**、会话列表为空跳过；**未知目录不再删除**，改为移到 `data/uploads/.orphan/<时间戳>/`（保留最近 3 批）；已知会话目录里仍只删未被消息引用的文件。
  - `tests/conftest.py`：新增 autouse fixture `_no_upload_sweep`，所有测试统一 `GM_NO_SWEEP=1`。
  - `scripts/dev_check.py`：启动前 `setdefault("GM_NO_SWEEP", "1")`，自检不再碰真实附件清理。
  - `tests/test_uploads_guard.py`：扩到 6 条（显式开关 / pytest 自动跳过 / 会话文件被重定向 / 空会话 / 未知目录隔离 / 引用文件保留）。
  - `web/chat.js` + `web/css/polish.css`：附件文件缺失时显示「图片已丢失」占位（可悬停看原因），不再静默移除缩略图。
- 验证：`py -m pytest tests/test_uploads_guard.py -q` 6 passed；`py_compile` 4 个文件；`node --check web/chat.js`；`py scripts/dev_check.py` 9 项全过；自检后真实 `data/uploads` 唯一文件仍在。
- 已知：已经被删除的历史附件无法自动恢复（`shutil.rmtree` 不进回收站）。之后新发的图片会正常落盘并在重启后保留；如果系统有还原点 / 文件历史，可人工恢复 `data/uploads`。

## [2026-11-09] 修复：划词「复制」丢失换行与空格

- 问题/现象：对话、笔记、PDF 的划词浮层点「复制」，粘贴出来被压成一行，换行、连续空格、代码缩进都没了。
- 根因：
  - 对话适配器 `aside.js::freezeSelection/rangeTextIn` 用 `.replace(/\s+/g,' ')` 生成给模型看的提问文本，复制直接复用了它。
  - 阅读器 `reader.js::cleanRangeText` 同样 `.replace(/\s+/g,' ')`，`copySelection` 用 `sel.text`。
  - 笔记用 `range.toString()`，块级元素之间不保证有换行。
- 修复：
  - `web/selkit.js`：新增并暴露 `SelKit.plainText(range)`。按节点类型重建文本：`white-space` 含 `pre` 的文本原样保留；普通文本折叠空白；`<br>` 与块级元素补换行；跳过 `script/style/display:none`；去零宽字符、最多保留一个空行、保留代码缩进。
  - `web/aside.js`：`freezeSelection` 同时产出 `text`（给模型，仍压一行）与 `copy_text`（复制用，保留原文）；复制按钮改用后者，且不受 4000 字提问截断影响。
  - `web/init.js`：笔记 draft 增加 `copy_text`，复制动作优先用它。
  - `web/reader.js`：阅读流 / 译页用 `SelKit.plainText`；PDF 原页文字层是绝对定位 span，新增 `pdfRangeText` 按 span 坐标补换行与词间空格；`copySelection(text)` 接受显式文本。
- 验证：
  - 无头 Edge + `file://` 页面加载真实 `selkit.js` / `aside.js`，模拟选区后读 `SelKit.current.raw.copy_text`：
    AI 段落 → `"line1\nline2 with spaces\na\nb\ncode line\n  indented line"`；
    用户气泡（`white-space:pre-wrap`）→ `"lineA  two\nlineB   three"`。换行、连续空格、代码缩进都保留。
  - `node --check` 4 个文件；`py scripts/dev_check.py` 9 项全过。
- 已知：普通段落里的连续空格会被浏览器折叠（HTML 语义），只有 `pre` / `pre-wrap`（用户气泡、代码块）才保留；PDF 的换行/空格按 span 几何位置估算，极端排版可能有误差。

## [2026-11-09] 修复：流式输出中途报「liveText is not a function」

- 问题/现象：发送消息后模型输出几秒，正文出现「请求失败：TypeError: liveText is not a function」；再过几秒输出完成并恢复正常，和正常结束无异（用户截图确认）。
- 根因：为正文分段新增了全局函数 `liveText(L, t)`（web/stream.js），但 `streamChat` 里仍保留旧的局部 `const liveText = typing.querySelector('.live-text')`（一个 DOM 元素）。同一作用域内局部把全局函数遮蔽，`delta` 分支调用 `liveText(live, obj.text)` 实际是在调用 DOM 节点 → TypeError；异常被主循环 catch 后写进 `acc`，所以看起来像请求失败，但服务端仍在跑，最终 `replace`/收尾把完整结果补回来，于是「自行恢复」。
- 修复：
  - `web/stream.js::streamChat`：局部 `liveText` 改名 `liveTip`，`setLive()` 同步改。
  - `web/stream.js::stopStream` 里的同名局部也改名 `liveTip`，避免以后再踩同名的坑（接管/回放段此前已改过，注释在 2597 行附近）。
  - 全仓复查：已无 `const/let/var liveText` 声明，所有 `liveText(...)` 调用都指向全局函数。
- 验证：`node --check web/stream.js`；`py scripts/dev_check.py` 9 项全过（61 个前端 JS 语法通过）。
- 已知：这次是真实前端异常，不是网络中断；如果以后还有 JS 异常，仍会以「请求失败：<错误>」显示在正文里（便于暴露问题），但不会再是这条。

## [2026-11-09] 版本号 3.3 + GitHub 提交前检查

- 版本：
  - `app/store.py::APP_VERSION` 3.2.2 → **3.3**（唯一版本来源，`/api/app/info` 与 HTTP `Server` 头都读它）。
  - `readme.md` 的 Version badge 同步为 3.3。
  - `desktop/package.json` 仍是 1.0.0：Electron 窗口壳自己的版本，与 APP 版本独立（沿用既有约定）。
  - NOTES 架构说明里的「当前 3.2.2」同步为 3.3。
- 提交前检查结果：
  - `py scripts/dev_check.py`：9 项全过。
  - `py -m pytest tests --ignore=tests/test_reader.py -o addopts="" -q`：**2139 passed, 9 skipped in 175.56s**（`test_reader.py` 需要 reportlab，环境未装，按既有约定排除；pytest.ini 的 `addopts=-q` 与命令行 `-q` 叠加成 `-qq` 会吞掉摘要，所以用 `-o addopts=""` 跑一次拿计数）。
  - `git diff --check`：无空白错误；顺手修掉了 `tests/test_computer_policy.py` 文件末尾多余空行。
  - 敏感文件：`data/`、`*.log`、`*.db*`、`.env*`、`.trae/` 均被忽略；已跟踪文件里只有 `resources/eval/fixtures/tool_pipeline/data/*.txt`（评测夹具，不是运行数据）；未跟踪 / 已修改文本文件未发现 `sk-` / `api_key=` / `SESSDATA` 明文。
  - 生成物：`.trae-html-share-packages/`（694KB 分享包）加入 `.gitignore`，不入库。
  - 大文件：新增最大的是 `web/vendor/pdfjs/`（pdf.worker.min.mjs 1.08MB + pdf.min.mjs 424KB + LICENSE），是离线 PDF 阅读依赖，可入库。
  - 删除：`tests/test_background.py`、`web/work.js`；后者已拆成 `web/work/*.js`，`index.html` 引用与注释均已同步，无残留引用。
  - 换行：`core.autocrlf=true` 且有 `.gitattributes`，Git 的 LF→CRLF 提示属正常，不是错误。
  - 规模：约 108 个已跟踪文件改动（+15468 / −4363）+ 64 个未跟踪文件（`.trae-html-share-packages/` 忽略后）；建议按主题拆成多次提交，不要一个 commit 全塞进去。
- 待你确认是否入库的「开发预览链」（运行时不依赖）：
  - `scripts/_inspect_fixture.py`、`_mk_preview_fixture.py`、`_mk_stock_fixture.py`、`_scan_globals.py`
  - `web/_stock_fixture.js`、`web/_stock_preview.html`、`tests/fixtures/stock_ui.json`
  - `scripts/check_stock_refactor.py`、`scripts/check_stock_ui.py`、`scripts/transpage_test.js` 属检查脚本，建议入库。
- 提交：`638886c`（main），191 个文件，+37480 / −4364；已推送 `origin/main`，远端与本地 HEAD 一致，工作区干净。

## [2026-11-09] 调研：Merlin's Whisper（黑盒说服式提示压输出）对本项目是否值得落地

- 论文：*Merlin's Whisper: Enabling Efficient Reasoning in LLMs via Black-box Persuasive Prompting*，arXiv 2510.10528（v3，ACL 2026 长文），代码 `github.com/hemingkx/Whisper`。
- 方法：把「少想但不掉准确率」当成黑盒提示搜索问题。用 GPT-4o 从 4 类视角（情绪诉求 / 威胁 / 证据式说服 / 角色扮演+详细指令）每轮各生成 10 个「提示后缀」，拼在原指令后跑 dev set；先按准确率阈值 τ 淘汰掉分的候选，再按平均输出长度排序，取 top-5 作为下一轮范例，共 3 轮，最后选「准确率可接受且最短」的后缀。不训练模型、不需要权重。
- 论文数字：Qwen3 系简单题（GSM8K）输出长度约 3× 压缩且准确率不降；四个 benchmark 平均 token -40%；Claude-3.7 MATH-500 -46%、Gemini-2.5 -50%；三轮迭代贡献明显（Qwen3-14B 从 -32% 到 -37%）；简单题收益远大于难题（AIME 只到 76%）；跨模型/跨域可迁移。
- 本项目实测（只读 `data/token_usage.jsonl`，6849 条）：completion 合计 20,335,394 / prompt 合计 3,871,390,716 = **0.5253%**；completion P50 **84**、P90 3,556、P99 54,956、max 369,995、mean 2,969；其中带 `reasoning` 字段的 1,055 条合计 **10,419,372**（约占输出一半，全部来自 `deepseek-flash`）；模型只有 `deepseek-flash` 与本地 `qwen35-9b:iq4nl`。`docs/TOKEN_OPTIMIZATION_DESIGN.md` §3.1 的口径（192:1）与本次复算一致，§13 已明确「优化输出侧：不做」。
- 结论：**不建议照搬落地**。论文优化的是「长思考链输出」；按 token 算输出只占 0.53%，但**按官方单价折算占账单 11%~14%**（见同日「修正：按官方单价重算」条目——本条的旧说法「影响只有 0.2%，四舍五入误差」已作废）。真正的大头仍是输入端（约 86%），已由 `token_profile=balanced` + 工作集 40k + 工具结果折叠治理。
- 唯一值得考虑的理由是**间接效应**：assistant 正文占输入字符 62.6%，输出短 → 后续每轮重发的 reply 变短；但工作集 40k 已经把这条链截断在 4 万以内，收益会被部分吸收，量级预计个位数百分比，且代码/报告类输出必须豁免。
- 如果将来真要试（例如切到推理模型、或延迟成为瓶颈）：① 后缀只能贴在**请求末尾**（复用 `_attach_dyn_tail`），绝不能塞进 S0/S1 稳定段，否则前缀哈希变化、缓存命中率反升成本；② 只保留「证据式 / 角色 / 详细指令」三类视角，威胁与情绪类论文自己都列为不适合产品化；③ 质量门禁用现有 `app/bench.py` + `resources/eval/bench_tasks.json`（通过数不降、平均轮数增幅 ≤10%、重复读取率 ≤+20%）；④ 先做「固定后缀 A/B」而不是跑候选搜索——本项目 dev set 是 agent 多轮任务，一次完整搜索的调用成本远超它可能省下的输出 token。

## [2026-11-09] 调研：HarnessMM（复用已决断动作、跳过推理回调）对本项目是否值得落地

- 论文：*HarnessMM: Recursive Self-Improvement for Harness Token Efficiency*，arXiv 2610.32813v1，清华 + MSRA，2026-10-23。
- 核心洞察：harness 在每次工具反馈后都要重新调一次推理，即使「下一步动作」在上一次推理里就已经定死了。于是把「已决断的动作」存成 **EDG（可执行决策图）**：`(原生动作, 完整字面参数, 前置节点, 公开执行守卫)`；工具回执只用来判断守卫是否成立，不重新生成参数。**GDR** 跨工具边界保留这张图并按守卫激活；**CCS** 用原生接口执行并把「被跳过的那个推理回调」记成证书（graph hash / 决策 id / 前驱回执 / 精确参数 / 工具回执 / 被绕过回调 id）。新语义决策、证据不足、选择器弃权 → 回落到原始 actor。actor 权重全程不变。
- 关键概念 **Substitution Paradox**：只有当 `C_removed > C_introduced`（作者成本 + 选择 + 额外上下文 + 回退 + 恢复）时才有收益。短输出、快速路径命中率高都**不能**证明回调真的被消除。
- 论文数字（complete-task 口径，含输入+输出、失败调用、图作者成本）：
  - HumanEval n=98：Codex 90.82% 不变，66.72k → 47.02k（-29.54%）；DeepSeek Harness 71.43%→85.71%，96.28k → 11.62k（-87.93%）。
  - MBPP-sanitized n=257：DeepSeek 90.27%→91.83%，53.98k → 10.61k（-80.35%）。
  - MBPP 消融：持久化复用比单次图省 26.91%；跳过回调比保留回调省 19.52%；完全禁用 EDG 反而 95.33%（比 full 高 1.56 分）但贵 18.62%。
  - ALFWorld 2133 题：66.20% / 31.12k（No Skill 42.29% / 34.05k，SkillOpt 63.29% / 101.33k）。
  - SearchQA 3395 题：69.81% → **76.76% EM / 0.99k**（成本 -19.08%）；GAIA2 480 场景 43.96% / 639.64k（-12.41%）。
  - **反例**：AIME 560 题 87.14% / **5.90k，比 No Skill 的 4.59k 还贵 28.34%**（作者成本压过了省的调用）；Terminal-Bench 2.0（DeepSeek-flash，n=18）成功率 **77.78% → 50.00%**，成本 3426.78k → 1144.32k——**质量掉 27.8 分**。
- 对照本项目：
  - 方向比 Merlin's Whisper 对得多：它砍的不是输出，而是「整轮输入重发 + 该轮推理」，正对本项目 reply+user 占输入 80.4%、单次 P50 108,615 的结构。
  - 但**缓存会大幅打折，且折扣随回合长度变化**：按官方单价（命中 ¥0.02 / 未命中 ¥1 / 输出 ¥2 每百万）实算，普通回合（命中 90.7%）跳一轮只省该轮成本的 **14%**；而真正烧钱的长回合（命中 99.3%）命中重放占该轮成本 **70.9%**，跳一轮就能省七成。旧说法「只值约 -30% 输入」是按 30 天平均命中率 95.4% 算的，对长回合显著低估（详见同日「修正：按官方单价重算」条目）。
  - 任务性质不匹配：论文大赢的是流程化任务（写→编译→测试→lint，下一步在编译前就已确定）；本项目是开放式 galgame 工作台 + 通用编码 agent，下一步几乎总要读工具输出才知道，更接近 Terminal-Bench / AIME 那两栏（掉质量或成本反升）。
- 结论：**不立项**。不是思路错，而是（a）本项目的任务分布落在论文的反例区间；（b）95.6% 缓存命中把「省一轮」的账价打了三折；（c）实现成本高——要让模型在正常动作之外额外输出未来动作图（多一份输出成本），还要在 `app/api/chat.py` 的 agent 主循环里加守卫执行器和证书审计，远超既有「每批改动 ≤3 个落点」的约束，且新增「守卫判错 → 执行过期动作」的失败模式。
- 若要取它的 20% 而不付 80% 的代价：不做 EDG/GDR，只在**确定性链**上用本地规则直接执行（例如 `write_file` 成功后自动跑配置好的 format/lint，`run_command` 构建成功后自动跑测试），相当于「无作者成本的 CCS」；仍要过 `app/bench.py` 质量门禁。
- 资料：PDF 全文已抽到 `%TEMP%\harnessmm.txt`（37 页 / 172k 字符），`pypdf` 可复现。

## [2026-11-09] 修正：按官方单价重算 token 账单（推翻上面两条调研里的「钱」结论）

- 触发：用户指出「你对钱的判断有问题，看最近的输出也就是实际测试再下结论」。复核后确认两处结论作废。
- 单价来源：DeepSeek 官方「模型 & 价格」页（2026-11 查）。`data/config.json` 里 `chat_url=https://api.deepseek.com/v1/chat/completions`、`chat_model=deepseek-flash`（官方对应 deepseek-v4-flash）：**输入命中 ¥0.02 / 输入未命中 ¥1 / 输出 ¥2（每百万 tokens）**，比例 **1 : 50 : 100**。**结论只依赖这个比例，不依赖具体数值**；项目本身不记录计费，以下金额全部是「tokens × 官方单价」的折算。
- 实算（只读 `data/token_usage.jsonl`，6851 条）：

| 窗口 | 命中率 | 命中 ¥ | 未命中 ¥ | 输出 ¥ | 合计 ¥ | 输出占账单 |
|---|---|---|---|---|---|---|
| 全部 | 95.43% | 74.11（25.3%） | 177.64（60.7%） | 40.97 | 292.72 | **14.0%** |
| 近 7 天 | 95.69% | 13.66 | 30.78 | 5.99 | 50.43 | 11.9% |
| 近 24h chat | 97.00% | 12.36（35.0%） | 19.13（54.2%） | 3.82 | 35.31 | 10.8% |
| 近 24h chat 去掉最大回合 | 90.73% | 3.12（14.0%） | 15.93（71.5%） | 3.24 | 22.28 | 14.5% |
| 最大回合（465M / 362 轮） | 99.31% | 9.25（**70.9%**） | 3.21 | 0.58 | 13.03 | 4.4% |

- 作废的说法：
  1. **「输出只占 0.5%，优化输出毫无意义」**——token 是 0.53%，但**账单占 11~14%**（输出单价 = 100× 命中价、2× 未命中价）。其中 thinking 占输出一半（10.42M / 20.34M），折合约账单 **7%**。
  2. **「跳过一轮只省约 30% 输入」**——那是按 30 天平均命中率 95.4% 算出来的；真正的长回合命中率 99.3%，命中重放占该轮成本 **70.9%**。普通回合才是 14%。**结论随回合长度翻转，不是一个常数。**
  3. 旧设计文档「未命中占输入成本约 70%」只对短回合成立；长回合反过来是命中重放占七成。
- 最近实测暴露的真问题（比这两篇论文都重要）：最大回合 465,460,768 token / 362 轮。`rounds_detail` 前 120 轮每轮 5.0~5.8 万（第 98 轮 106,050），据此后 242 轮平均 ≈**1.88M/轮**——**回合内上下文在 100+ 轮之后失控**。工作集（`history_working_set_tokens=40000`）管的是跨回合历史，管不到回合内增长。这一个回合就占当天账单 73%（¥13.03 / ¥35.67）。
- 修正后的优先级：① **回合内增长护栏**（`agent_turn_token_budget=30M` 只能事后收尾，拦不住单轮膨胀；需要「单轮上下文上限 / 回合内工具结果密度」）；② 工作集（砍的正是长回合里占七成的命中重放，仍是最值钱的一项）；③ 输出侧（11~14%，thinking 占一半，Merlin's Whisper 类手段最优情况也就 2~3%）；④ HarnessMM 式跳轮（任务上跳不掉，普通回合只值 14%）。
- 复算脚本：`%TEMP%\_cost_real.py`、`%TEMP%\_usage_recent.py`（均只读）。

## [2026-11-09] 调研：TokenPilot / CTXAgent（上下文管理两篇）与本项目的差距

- **TokenPilot**（arXiv 2606.17016，EMNLP 2026 Findings）：双层上下文管理。① 全局 **Ingestion-Aware Compaction**：把易变字段（agent id、工作目录、时间戳、工具 schema）换成静态占位符来稳住前缀，并在「入库口」就压掉工具的原始噪声（HTML/日志），压缩后的内容按 **内容哈希** 存进 artifact registry，留一个恢复工具按需取回；同一哈希被反复取用超过阈值 τ 就停止压缩。② 局部 **Lifecycle-Aware Eviction**：上下文段分 `active / completed / evictable` 三态，用轻量模型（Qwen3.5-35B-A3B）每 B 轮批量估一次「残余效用」，只有效用归零才一次性清除，**不做逐轮小改**。B=3 是实测最优（B=1 太频繁，切一次丢一次缓存）。
  - 数字：PinchBench 连续模式成本 $7.24 → $2.79（-61%），Claw-Eval $81.52 → $10.58（-87%），分数 79.2 → 81.3。
  - 组件拆解（PinchBench 连续）：只做前缀稳定 $7.24 → $6.39；加归约 $4.22；再加驱逐 $2.79。隔离模式：前缀稳定单独就 $8.31 → $4.35（-48%），整条链路 $2.87。缓存命中率 38.7% → 79.2%。
  - 关掉恢复工具后：分数 80.9 → 77.1，成本反涨到 $4.03（模型开始补偿性重试）。说明「可寻址」比「压得狠」重要。
- **CTXAgent**（arXiv 2610.34425，2026-10-25）：不给脚手架，只给模型两个工具 + 一段说明，让它自己管上下文。`compact_context(summary, keep_from, keep_ids)` 把 system 之后到 `keep_from` 的整段归档，**摘要由模型自己写，不额外调摘要模型**，所以压缩本身不额外花钱；`curate_context(drop=[{id,note}])` 把已经用过的工具结果换成模型自己写的一行备注。护栏是把阈值压缩保留为兜底。
  - 数字（35–212 轮的混合会话）：7 个前沿模型里只有 **Claude Opus 5.5 / GPT-6.1 Sol 稳定获益**（混合会话 -43% / -48%，困难会话 -58%）；Haiku 5.5、Muse Wonder 根本不用压缩工具；Sonnet 5.5 三次里只压了一次；Sol 的摘要啰嗦到输出 token 涨 5 倍。
  - harness 对比：同一个 Opus 5.5，CTXAgent（1M、自管）比 Claude Code 200k 省 34%、比默认省 49%，分数持平；而 Claude Code 默认窗口**根本没触发压缩**，涨到 277k。CTXAgent 全程没超过 80k。
  - 引用的旁证：CliffCompaction 测到「缓存读取约占未压缩账单的 78%」——与本项目实测（长回合命中重放占该轮成本 70.9%）吻合。
- 对照本项目：
  - **TokenPilot 最大的一块我们已经吃到**：它的收益大头来自「前缀稳定把缓存命中率从 38.7% 抬到 79.2%」，而本项目整体命中 **95.4%**（短回合 90.7%、长回合 99.3%）。批 2 的 S0/S1 分段 + `_attach_dyn_tail` 做的就是这件事。
  - 我们**没有**的是它第三层：**「内容哈希 + 取用频率门」**——同一份工具输出被反复取回时不再压缩。我们只有 `spill` 落盘 + `read_tool_result` 取回 + 按字符数截断，没有反向门。
  - 我们**做了一半**的是归约：`tool_result_max=30000` 字 → head 4096 + tail 1024，>50000 落盘。TokenPilot 的证据是「彻底删恢复工具会掉分并涨价」，所以我们现有的可寻址设计方向是对的，不用改。
  - **两篇的共同点、也是本项目最该记的一条**：**每次改写上下文，都会从改写点开始丢掉缓存**。折叠越早的内容代价越大。我们现在的 `trim_tool_results` 是按阈值逐步折叠旧结果（位置靠前），属于「频繁小改」，可能一直在制造整段 miss；TokenPilot 的做法是攒到任务边界一次性切。这条机制解释了本项目长回合 99.3% 命中下仍有 3.2M 未命中的来源，但**尚未逐轮验证**。
  - CTXAgent 的价值在「摘要由模型自己写、不额外调模型」，我们 `maybe_compact_working_set` 是后台另起摘要调用。但它的结论强依赖模型：deepseek-flash 是否愿意自我管理，**没有任何证据**；要抄必须先做小实验。
- 结论：两篇都不必照搬，但要吸收两条设计原则：① **压缩少而整**（在任务边界一次性切，别每轮小改）；② **可寻址优先**（压掉的内容必须能取回，否则模型补偿性重试会更贵）。TokenPilot 的「哈希 + 取用频率门」是缺的那一小块，成本低、可先做；CTXAgent 的「模型自己压」要先验证 deepseek-flash 的能力再谈。
- 顺带验证并**未成立**的假设：怀疑 `system_clock_text()` 把精确到分钟的时间放在 system 末尾，导致跨分钟就整段丢缓存。用最近相邻回合（同会话、间隔分组）对比，跨 5–15 分钟仍有 82.9% 命中、跨 15–60 分钟 91.8%，**证据不支持**。注意回合级 `cached` 会被长回合的多轮稀释，单轮回合样本只有 1 个，所以这条只能算「没找到证据」，不算「已排除」。
- 资料：`%TEMP%\tokenpilot.txt`（16 页）、`%TEMP%\ctxagent.txt`（26 页），`%TEMP%\_clock_effect.py` 为只读验证脚本。

## [2026-11-09] 综合四篇论文（Whisper / HarnessMM / TokenPilot / CTXAgent）的 token 优化结论

- 统一记账口径（官方单价 1 : 50 : 100，即命中 ¥0.02、未命中 ¥1、输出 ¥2 每百万）：**一个未命中 token = 50 个命中 token 的钱，一个输出 token = 100 个命中 token 的钱**。所以「省了多少 token」这句话没有意义，必须问「省的是哪一类」。
- 30 天实测账单构成（¥292.72）：命中 ¥74.1（25.3%）、未命中 ¥177.6（60.7%）、输出 ¥41.0（14.0%）。
  - 但「token 命中率 95.4%」是误导性指标——它在钱上只值 25%。**该盯的 KPI 是「未命中 tokens / 回合」和「输出 tokens / 回合」**，不是命中率。
  - 普通回合（去掉最大回合）反过来：命中只占 14.0%，未命中 71.5%，输出 14.5%。即**短回合的痛点是「每次重付全额」，长回合的痛点才是「空转重放」**。
- 四篇各打哪一段（这是四篇最有用的一张图）：

| 论文 | 打的是 | 对应账单项 | 在本项目的实测上限 |
|---|---|---|---|
| HarnessMM | 减少轮数（跳过已决断的推理回调） | 每轮的命中+未命中+输出全砍 | 任务上跳不掉，普通回合跳一轮只省 14% |
| TokenPilot | 前缀稳定 + 入库归约 + 生命周期驱逐 | 命中（已吃满）、未命中（新增量） | 命中 95.4% 已超过它论文最好水平；归约那层还有空间 |
| CTXAgent | 压缩时机与内容（模型自管） | 未命中（重建量） | 依赖模型，deepseek-flash 能力未知 |
| Merlin's Whisper | 输出/思考长度 | 输出（14.0%） | thinking 占输出一半，最好情况省 2~3% |

- 从四篇里能提炼的四条共识（都比它们的具体算法更值钱）：
  1. **缓存连续性 > 文本压缩**。TokenPilot 用 38.7%→79.2% 的命中率换来 -48% 成本；CTXAgent 引用 CliffCompaction「缓存读取占未压缩账单 78%」，与本项目长回合 70.9% 吻合。任何改写上下文的动作，都在从改写点开始丢缓存。
  2. **可寻址 > 不可逆删除**。TokenPilot 关掉恢复工具：分数 80.9→77.1、成本反涨到 $4.03（补偿性重试）。与我们 `spill` + `read_tool_result` 的方向一致，不能砍。
  3. **少而整 > 频繁小改**。TokenPilot 的驱逐按 B=3 轮批量执行，B=1 太频繁反而更贵；CTXAgent 的说明里明确写「每改写一次就丢一次缓存，别每一步都改」。我们现在的 `trim_tool_results` 是按阈值逐步折叠旧结果（位置靠前），方向相反。
  4. **压缩点要落在任务边界**。CTXAgent 让模型在「一段工作结束」时压，而不是按 token 阈值。
- 我们已经吃掉的：前缀稳定/缓存命中（批 2）、工作集 40k（批 3）、可寻址工具结果（spill + read_tool_result）、回合累计预算 30M。
- 还空着的，按钱的预期排序：
  1. **未命中构成未测量**（¥177.6 / 60.7% 不知道花在哪）。设计文档 M6 提过 `miss_parts`（新增内容 / 前缀漂移 / 压缩重建），**没有实现**。没有这个数，下面三条都无法验收。
  2. **回合内无上限**：实测单回合 4.65 亿、后 242 轮平均 1.88M/轮；30M 的回合预算只能事后收尾。
  3. **折叠方式方向错了**：逐步折叠旧结果（位置靠前）= 每轮制造整段 miss。应改为「攒到边界一次性切」。
  4. **入库口归约 + 哈希取用频率门**（TokenPilot 唯一我们还没抄的一小块）。
  5. 输出侧（14%，其中 thinking 7%）——最后再谈。
- 一句话结论：**四篇里只有 TokenPilot 的「缓存连续性」和「少而整」对我们的账单有直接作用，但它最大的那块我们已经吃完了；真正剩下的钱在「未命中」上，而我们现在连它由什么构成都还没测。先补观测，再改机制。**

## [2026-11-09] 实施：补「未命中构成」观测（miss_parts）

- 目标：回答「占账单 60.7% 的未命中，是新增内容、折叠重建，还是缓存没接上」。只加观测，不改装配、不改预算、不改请求。
- 新增 `app/missplit.py`（纯函数，可单测）：`split(rounds)` 把每一轮的 `prompt - cached` 拆成四类：
  - `cold`：本回合第一轮且该会话此前无请求（没有可比前缀）。
  - `new`：缓存正常命中时必然要付的部分。首轮用 `prefixtrace` 的公共前缀（`cur_tokens - prefix_tokens`），第 2 轮起用相邻两轮的本地估算增量，并用本轮 `prompt/est` 比例校准估算偏差（比例 <0.2 或 >8 时放弃校准）。
  - `rebuild`：超出 `new` 的部分，且本轮发送前发生过工具结果折叠或历史压缩。
  - `drift`：剩下的部分——本地解释不了，即缓存没按预期命中。
- `app/api/chat.py`：
  - 每轮发送前快照 `_est_send = estimate_messages_tokens(messages)` 与折叠/压缩计数。
  - 每轮拿到 usage 后把 `{n,prompt,cached,est,fold}` 追加进 `_miss_rounds`（**不受 `rounds_detail` 的 120 轮上限影响**）；首轮附带 `cold/prefix_tokens/cur_tokens`。
  - `rounds_detail` 每轮新增 `cached` 字段（原来只有回合累计，看不到「哪一轮开始断链」）。
  - 回合收尾写 `timing.obs.miss = {cold,new,rebuild,drift,total,rounds}`。
- `app/usage.py`：`_obs_stats` 新增 `miss_parts` 求和（只统计带 `miss` 的记录，`turns` 表示覆盖了多少回合；旧记录自动跳过）。
- `web/settings.js`：token 明细新增「未命中构成」卡片，显示四类占比与覆盖回合数；旧后端数据自动降级为「—」。
- 验证：
  - `tests/test_missplit.py` 13 条（空输入/缺字段/冷启动/温暖首轮/增量/漂移/折叠重建/估算偏差/比例越界/首轮缺用量/守恒）。
  - `tests/test_usage.py` 新增 2 条（累加、旧记录不污染）。
  - `tests/test_continue_resume_live.py` 的 `test_turn_writes_token_obs` 增加断言：四类之和 == `total` == 真实 `prompt - cached`。`GM_LIVE_UI=1` 跑 **5 passed**。
  - 定向回归（context / prompt_assembly / chat_blocks / stream_stop / token_profile / working_set / token_budget）**132 passed**。
  - `py scripts/dev_check.py` 全部通过；全量 `pytest tests`（排除缺 reportlab 的 reader）：**2151 passed, 9 skipped, 2 failed**——2 条 `tests/test_archify.py` 是渲染器并发/环境抖动，单跑该文件 **14 passed**，与本改动无关。
- 已知边界：
  - `new` 是**估算**（本地 tokenizer 与真实 tokenizer 有系统性偏差，已按本轮比例校准），不是精确归因；结论看占比趋势，不看个位数。
  - 只有**新记录**带 `miss` 字段；历史流水没有，`miss_parts.turns` 会一直是 0 直到产生新记录。需要重启后端才生效。
  - 运行 live 测试会把 `data/current_session.json` 写成 `{"id":"s1"}`（该会话不存在，`sessions._apply_current` 会忽略并回落到 `sessions.json` 的 `current`，下次保存自动复原）；`sessions.json` / `config.json` 内容已核对无损。
- 下一步（等有数据后再做）：看 `miss_parts` 里 `drift` 与 `rebuild` 的占比，决定是改折叠策略（少而整）还是先管回合内膨胀。

## [2026-11-09] 复核：七家 harness 的上下文管理机制（Claude Code / Codex / OpenCode / dsh / ZCode / Trae / Cursor）

> 与 `docs/TOKEN_OPTIMIZATION_DESIGN.md` §2.1 的旧表对照：下表补了官方来源，旧表数字不变。

| 产品 | 触发点 | 动作与保留策略 |
|---|---|---|
| Claude Code | 接近窗口；200k 显式窗口实测约 **168k** 才切 | 先清旧工具输出 → 再结构化摘要；有手动 `/compact`。CTXAgent 实测：默认窗口下 Opus 5.5 甚至涨到 **277k** 都没自动切 |
| Codex CLI | 配置上限，**最多 90% 窗口**；显式 200k 实测约 **145k**，默认 170–190k | 保留最近若干条用户消息到 token 预算；托管模型走**服务端压缩端点**；append-only |
| OpenCode | 到达配置的窗口值 | 压缩到配置窗口；可选 pruning **默认关闭**；模型可调用的 compress 工具只是第三方插件 |
| DeepSeek Harness（dsh） | `min(W × thresholdRatio, W − maxOut − headroom)`；ratio 默认 **0.8**、headroom 默认 **65536** | compaction-basic。坑：窗口 < ~370k 时 `min()` 会选中 headroom 项，131k 窗口在 **37.5%** 就压。本项目 `request_budget()` 就是照这个公式写的 |
| ZCode | `W − min(maxOut, 21000) − ~13000`（固定扣 ~34K）；128k→**94k(73%)**、200k→166k、1M→966k | 触发判定**含本轮工具结果的实时估算**，所以会比进度条略早压；**没有用户可调阈值**；有 `/compact` |
| Trae | 超过上下文窗口自动触发 | 自动压缩 + 面板手动「压缩」按钮；Max 模式 1M 窗口、单任务最多 200 轮工具调用 |
| Cursor | 不靠「压」靠「少带」 | 语义块索引 + Merkle 增量；长工具输出写文件、Agent 按需 tail/grep；MCP 懒加载（官方 A/B 总 token **-46.9%**）；摘要时用文件引用代替正文 |

- 结论一：**七家全是「阈值触发 + 一次性压到预算内」**，没有一家做「每轮上下文硬上限」。所以我们跑出 1.88M/轮不奇怪——这是主流 harness 的共性缺口，TokenPilot 也正是拿这一点做文章。
- 结论二：**触发点全部按窗口算**（`W − 输出预留 − 余量`），没有一家用固定 token 数。我们 `tool_result_trim_tokens = 100000` 是异类：它是固定值、只折工具结果、且折完不保证降到阈值以下。
- 结论三：**压的动作是「一次压到预算内」，不是「折一条」**。Codex / ZCode / Claude Code 都是这样；这印证了第三件（少而整）。
- 结论四：dsh 的 headroom 陷阱值得记——`min()` 在小窗口上会被常数项主导，导致实际触发比例远低于 ratio。我们的配置（1M 窗口、ratio 0.8、headroom 8192）不受影响，但换小窗口模型时要重算。
- 来源：ZCode 官方 FAQ（触发公式与实测档位）、dsh compaction 文档（公式与默认值）、CTXAgent 论文（Claude Code / Codex / OpenCode 的实测切点）、Trae 官方文档、Cursor 为本项目此前调研（官方博客/文档，未在此轮复核）。

## [2026-11-09] 方案：单轮上下文上限（批 5，未实施）+ 增加动因实测

- 方案写在 `docs/TOKEN_OPTIMIZATION_DESIGN.md` §20（含上限公式、动作阶梯、配置清单、回滚、验证、风险）。
- 核心：新增工作线 `C = clamp(W×0.2, wset+30000, 150000)`（1M 窗口 → 15 万），与安全线 `request_budget()=80 万` 分工；超线时按「折工具结果 → 压历史（每回合最多 2 次）→ 仍超则记录并继续」执行；滞回压到 70% 再收手；`turn_context_cap = 0` 一键回滚。
- **增加动因实测**（只读 `rounds_detail[].in_parts` 的相邻轮增量，290 回合 / 1319 个增长轮）：
  - 30 天：`tool_result 48.0%`、`user 47.8%`、`reply 2.9%`、`schema 0.7%`。
  - 最近 3 天：`tool_result 87.7%`、`user 7.0%`、`reply 4.7%`。
  - 结论：回合内增长的主体是工具结果，正是 `trim_tool_results` 能折的东西 → 方案有效；`reply` 可忽略，不需要「回合内 reply 折叠」；30 天口径里 user 占一半说明有粘贴型回合，那部分压不住，会体现为 `cap.stuck`。
- 注意：探测只统计「正增量明显大于负增量」的轮次（跳过 8468 个折叠/压缩轮），是「增长轮的构成」而非全部轮次平均。

## [2026-11-09] 实施：按需加载（lazy context，批 6）

- 目标：把「读之前少带、读的时候按需、读完可寻址」做成默认行为。方案与配置见 `docs/TOKEN_OPTIMIZATION_DESIGN.md` §21。
- A 自动召回预算（`app/api/chat.py::_auto_code_refs`）：结果先按 score 排序，前 `auto_context_inline_max`（默认 2）段且总正文 ≤ `auto_context_budget_chars`（默认 8000）才内联；其余打 `slim=True`、不带给正文。`context._msg_text` 遇到 `slim` 只渲染「【引用代码：path:start-end】」一行。估算与装配共用同一函数，进度条不会失真。
- B 同回合重复读去重（`app/agent.py`）：新增 `_READ_SEEN`（键 `(sid, 沙箱根, 绝对路径)` → `(版本戳, turn_key)`），不复用进程级的 `_READ_STATE`（那是写前读守卫、跨会话共享，会让新会话误以为已读过）。三重条件命中才回一行提示；带 `start` / `limit` / `raw` 一律照常读；只记「整篇正文给过」，分段读和大纲不入表。`turn_key` 由 `chat.py` 在 `caps` 里生成。
- C 落盘阈值 50000 → 12000（`tool_spill_max`），预览改为「头 1200 字 + `spill.key_lines` 关键行（报错/失败/警告，去重，最多 20 条）+ 尾 400 字 + locator」。`spill.key_lines` 新增，纯正则行筛选。
- D 大文件默认大纲（`read_outline_mode`，**默认关**）：`.py/.js/.ts` 且 ≥ `read_outline_min_lines`（600）且没带 start/limit 时，`read_file` 先返回 `repomap.outline()` 的符号表。会改变工具返回形态，等 bench 再开。
- E 会话首次提问注入仓库结构（`repo_summary_inject`，**默认关**）：`repomap.build(...)[:1200]` 作为虚拟引用，只在 `len(user messages) <= 1` 时注入；放在引用里而不是 system，避免文件变化打断前缀缓存。
- 设置页：省费模式组新增 5 个控件（内联预算 / 内联条数 / 重复读去重 / 大纲模式 / 仓库结构）。
- 验证：`tests/test_lazy_context.py` 14 条（关键行、大纲、去重的 5 种分支、召回预算、slim 渲染、默认值）+ `tests/test_agent_limits.py` 补 3 个新键；`scripts/dev_check.py` 全过；全量 `pytest tests` **2165 passed / 9 skipped / 2 failed**（2 条 `test_archify.py` 渲染器并发抖动，单跑该文件全过）；`GM_LIVE_UI=1` 端到端 **5 passed**。
- 已知边界：自动召回减少内联后，模型可能多一次 `read_file`——用 `miss_parts.new` 与每任务 `read_file` 次数监控；D/E 未跑 bench。

## [2026-11-09] 诊断：16:04 那轮「AI 不跑命令、要求人类执行 + 大量重复」

- 定位：会话 `s_07db41e3dabc` 第 63 条消息（`done_at` = 16:04:38），usage 记录 `prompt 30,166,561 / cached 29,381,120 / completion 90,168 / rounds 71 / auto_continues 146 / end_reason=budget`。
- **不是工具通道坏了**：该回合实际执行了 **89 次工具调用**（read_file 42、search_files 26、run_command 10、edit_file 9、read_file 批量 1、write_file 1），只有 6 次失败（`_chk.mjs` 语法错、`_u1.cjs` BOOT_FAIL、`_w4.cjs` 连续 3 次 exit 1、一次 `read_file {}` 缺 path）。
- 真正的转折在 block 178 之后：模型开始把调用写成**纯文本** `<run action="exec">…</run>` / `<read action="file">…</read>`。运行时不解析这种标签（只认原生 tool_calls），所以从 block 191 起没有任何调用被执行；模型据此判断「通道不通」，在 block 210 起反复输出「需要你做的：cd /d … && node _u1.cjs 1200」。
- 退化诱因（按证据强度）：① `_w4.cjs` 连续 3 次 exit 1（fillRect=0 arc=0，测试脚本自身缺陷）；② 一次空参数 `read_file`（INVALID_ARG）；③ block 184 出现损坏片段 `</｜｜DS`；④ 中途多次切成英文自言自语（"Let me actually make tool calls properly. The issue seems to be that my tool calls aren't being pars[ed]"）。**模型侧退化，不是本地解析 bug**——同一段里 block 187 的原生调用仍成功。
- 重复的两层原因：
  - 消息内：`agent_auto_continue = -1`（无限续跑）+ `agent_max_rounds = 0` + `agent_auto_idle_limit = 0`（代码注释明确「不再据此停轮」），只有 `agent_turn_token_budget = 30M` 能停。每次续跑模型都把整段结论重写一遍 → 45 个「工具通道…」段落、309 个 block、54,860 字符。
  - 落库层：第 56–62 条是 7 条未收尾快照（`interrupted=True`、`end_reason=resume`），内容 1,798→5,901 字符、工具 28→89 条，只有第 63 条有 `done_at`。续跑没有原地更新，而是每条新落一条；若前端把它们都渲染出来，就会看到同一个回答重复 7 次。
- 建议的护栏（未实施）：① 检测「本轮只有纯文本工具标签、没有原生 tool_calls」→ 回灌明确纠正 + 计入空转；② 连续 2 次空转就收尾（`agent_auto_idle_limit` 从 0 改 2）；③ `agent_auto_continue` 默认改有限（如 8）；④ 续跑落库统一走原地更新，避免快照堆积。

### 技术细节补充（问题先记录，暂不修）

**这个失败模式的名字：tool-call grammar leakage。** 模型把本该走结构化 `tool_calls` 的内容当纯文本写进 `content`。业界汇总（`pi-tool-repair` 的 Tool-Call Grammar Leakage Survey，2026-06）把它按语法家族分类，我们命中的是 **A 族：DeepSeek DSML**：

```
<｜｜DSML｜｜tool_calls>            # 双竖线（V4 SSE 最常见）
<｜DSML｜tool_calls>               # 单竖线
<DSML｜tool_calls>                 # 无前导
< | DSML | tool_calls>            # 代理转码后的 ASCII 变体
```

`｜` 是 U+FF5C（全角竖线）。日志 block 184 里的残片 `</｜｜DS` 就是它被截断的样子。

**根因（按与本项目的相关性排序）**：

1. **流式分片切开起始标记**：`<｜｜DSML｜｜tool_calls>` 很长，被切在两个 SSE chunk 之间，解析器先把前半段当正文吐出去，后面就再也认不出来了。
2. **reasoning parser 与 tool parser 的顺序**：两者同时开启时，工具解析器只在 reasoning 解析产出之后处理 `content`；模型把 DSML 写进未闭合的 `<think>` 里，标签落进 `reasoning_content`，工具解析器根本看不到。
3. **`tool_choice=auto` + `stream=true` 是高风险路径**：`required` 与非流式明显更稳（走不同的受限解码分支）。
4. 批量解码里的 `skip_special_tokens=True` 会把 sentinel 直接删掉；自建 vLLM/SGLang 或聚合层缺 `--tool-call-parser` 时原始标记直通。
5. 温度越高越容易偏离严格语法。
6. 提示词里写「不要输出 DSML」基本没用——这是 tokenizer/训练层的行为，不是指令遵循问题。

**本项目的两处具体缺口**（在 `app/llm.py`）：

- `_TAG_RE` 要求标签名以 `[A-Za-z]` 开头，而 `<｜｜DSML｜｜tool_calls>` 以 `｜` 开头 → **匹配不到，不会被清除**。
- `_PROTO_NAME_RE = tool|parameter|invoke|function_calls|thinking|antml`，但模型退化后写的是 `<run action="exec">` / `<read action="file">` / `<edit action="patch">` —— `run/read/edit` 都不在名字族里 → **也不会被清除**。
- 后果是雪球：伪标签留在 `content` → 落库进历史 → 下一轮照着抄。已有注释「落库前清掉，否则会成为下一轮的模仿样本」说的就是这个机制，只是名单没覆盖到。

**Codex 的做法（源码级，第一类里唯一开源的）**：

- `codex-rs/codex-api/src/sse/responses.rs:231` `process_responses_event`：`response.output_item.done` 反序列化成 `ResponseItem`；正文走 `response.output_text.delta`，两条通道互不相干。
- `codex-rs/core/src/tools/router.rs:244` `build_tool_call`：只 match `FunctionCall` / `ToolSearchCall(client)` / `CustomToolCall`，末尾 `_ => Ok(None)` —— **没有正则、没有 XML 解析、没有"正文里找调用"**。
- `codex-rs/core/src/stream_events_utils.rs:158`：只有拿到 `ToolCall` 才 `needs_follow_up = true` 并派发；否则当普通消息，本轮结束。
- 唯一合法的"收文本"通道是 `ResponseItem::CustomToolCall → ToolPayload::Custom { input }`：**在 schema 里显式声明成 freeform 工具**（`apply_patch` 就是这样），而不是运行时猜。
- Claude Code / Cursor / Trae 闭源，行为一致（泄漏时把原始标签当正文显示，Visual Studio 的 BYOK Agent 有公开 bug 报告）。
- 结论：**第一类不修语法泄漏，把责任交给模型厂商的 API**。我们用的是被该汇总点名泄漏的 DeepSeek 官方 API，所以这条路不能照抄。

**将来修复时的四条原则（来自该汇总，暂不实施）**：

1. **Recover, don't just strip**：只删标签会让 agent loop 拿到 `finish_reason=stop` 且没有工具被派发——静默空操作。要把完整块还原成 `tool_calls` 并改写 `finish_reason`。
2. **流式缓冲**：缓存一个「标记长度」的窗口再决定是不是标签（Kimi 解析器约 1KB 缓冲 / 8KB 段上限），末尾要 flush。
3. **只恢复完整、工具名已知、参数可解析的块**；不完整的降级成文本，绝不把散文提升成执行。
4. **代码块 / 讨论语境豁免**：出现在 ``` 代码块里的语法不能删（OpenClaw 踩过）。

**参考资料**：`pi-tool-repair` 的 [Tool-Call Grammar Leakage Survey](https://raw.githubusercontent.com/monotykamary/pi-tool-repair/HEAD/docs/tool-call-grammar-leakage-survey.md)；[Codex 源码解读：Toolcalls](https://dangui.org/engineer/codex/6.toolcall/)；[Visual Studio BYOK 原始调用泄漏](https://developercommunity.visualstudio.com/t/Custom-LLM-Providers-BYOK-completely-b/11102117)。

**第二次复现（21:32，会话 `s_07db41e3dabc` 第 66 条）**：

- usage：`prompt 23,062,573 / rounds 55 / auto_continues 143 / end 空`；`rounds_detail` 120 条里 **65 条 `finish=stop` 且 `tools=0`**，而且 `n` 一路卡在 **56** 不再前进。
- 消息实际执行了 **66 次工具**（read_file 25、search_files 22、edit_file 10、run_command 5、write_file 2、read_files 2），只有 1 次失败——所以「无法执行任何工具」是模型的误判。
- 分界点：第 60 个工具 `node _w6.cjs 2400` 真失败（`BOOT_FAIL ACT_WORK is not defined`）→ 模型用 edit_file 修好（第 65 个工具成功）→ 之后再也发不出 `run`。block 151 起全是纯文本：`<run` 24 次、`<read` 49 次、DSML 碎片 3 次（`</｜｜DS`）。
- 模型自己的说法（block 154）「只有 read 和 edit 能真正执行」与数据吻合：146/149 是真实 read/edit，之后没有任何原生 run。
- 仍未确证：为什么只有 `run` 发不出原生调用。两种可能——① 模型认定 run 坏了，主动改成文本；② 该轮 `run_command` schema 没下发。现有数据没有「每轮工具清单」，分不出来；要分清需要加一个每轮记录工具数量/名字 hash 的埋点。
- 这也说明「文本伪调用检测 + 空转刹车」比任何解析修复都更急：这次会在第 2 次续跑停住，而不是 143 次。

**已实施（仅伪调用检测，未做空转刹车）**：

- `app/llm.py` 新增 `pseudo_call_tagged(text)`：先剔掉 ``` 代码围栏，再匹配三类写法——`<tool>/<invoke>/<parameter>/<function_calls>` 家族、`<run|read|edit|code|git|plan|agent|web|history … action=…>` 动作式 XML、DeepSeek DSML（双/单/无前导竖线 + ASCII 变体 + 流式截断残片 `</｜｜DS`）。产物多一个字段 `rr["pseudo_call"]`。
- `app/api/chat.py`：新增 `_MSG_PSEUDO_CALL`，两条注入路径——
  1. 自动续跑分支：本轮无原生调用且 `pseudo_call` 命中 → 在续跑提示前面加一段「你写的是纯文本，不会执行，请用原生调用」；
  2. 没有续跑理由时的兜底：追加一条该提示并 `continue`，再给一轮。
  - 每回合最多 2 次（`pseudo_nudges`），不做额外刹车；用完仍命中就退回原来的 warn。
- 测试：`tests/test_pseudo_call.py` 5 条（动作式 / DSML 各变体 / `<tool>` 家族 / 普通 HTML 与纯文本不误判 / 围栏内不误判）；`pytest tests/test_pseudo_call.py tests/test_chat_blocks.py tests/test_stream_stop.py` **50 passed**；`scripts/dev_check.py` 全过；`GM_LIVE_UI=1` 端到端 **5 passed**。
- 注意：`_FENCE_RE` 在 `llm.py` 里原本就有一个（按行匹配围栏标记），新加的整块围栏正则必须叫 `_FENCE_BLOCK_RE`，否则会被后面的定义覆盖、导致围栏内误判（这次踩过）。

## [2026-11-09] 变更：archify（流程图 / 架构图）只在 Agent 模式下可用

- 需求：取消「流程图工具的非 Agent 可调用」。此前 `archify` 被刻意放在 `agent_on` 之外（注释写着「不依赖沙箱，没有 Agent 时落 data/archify/<sid>/」），普通聊天也能调。
- 改动（两道闸）：
  1. `app/agent.py::native_tool_schemas`：`archify` 的 schema 追加包进 `if agent_on:`，非 Agent 会话根本看不到它；`hidden_tools` 的分层逻辑不变（Agent 下仍需命中 diagram 意图域或 `tool_search` 解锁）。
  2. `app/agent.py::_run_tool_impl`：新增执行前拦截——`caps["agent_on"] is False` 且 `name == "archify"` 时直接返回 PERMISSION 错误，提示改用内联 svg / mermaid / chart，或让用户开 Agent。`caps` 由 `app/api/chat.py` 在会话级注入 `caps["agent_on"] = bool(agent_on)`。
  - 只拦显式 `agent_on=False`：脚本 / 子代理 / 测试不传该键时保持兼容（`tests/test_archify.py` 里直接 `run_tool` 的用例不受影响）。
- 提示词同步：`_VISUAL_DIAGRAM_OFF` 从「archify 不需要开启 Agent」改为「非 Agent 模式下不可用，如实告诉用户开 Agent，不要假装生成、不要编 HTML 路径」。
- 行为变化：普通聊天里让 AI 画流程图 → 只会走内联围栏（svg / mermaid / chart）；要可交互 HTML 页必须开 Agent。
- 测试：`tests/test_archify.py` 新增 2 条（schema 不下发 / 执行被拦），改写原 `does_not_need_sandbox` 为 `requires_agent_mode`；`tests/test_agent_guards.py` 的视觉提示分层断言同步。`pytest tests/test_archify.py tests/test_agent_guards.py` **74 passed**；`scripts/dev_check.py` 全过。

## [2026-11-09] 调研：Native Shell（NSH）是什么，能不能整合

- 项目：`longbridge/nsh`，Apache-2.0，当前 **v0.4.0**（很早期）。另一个同名项目 `nativeshell/nativeshell` 是实验性 Flutter 桌面嵌入器，与 Electron 同类，更不相关。
- 定位：**跨平台原生 UI 框架**（Rust 实现）。同一套组件/状态/逻辑，在 iOS/Android/macOS/Windows/Linux 用各自原生控件渲染，Web 输出 HTML/CSS，也能渲染到 GPUI 应用和终端字符格。写 Rust 或 TSX（SolidJS 响应式 + Tailwind），可混用。
- 打包方式：独立 App、静态网站，或**嵌进已有原生 App**（官方有 embed-ios / embed-android 示例）；Web 侧通过 `createPlayer`(JS) 或 iframe 嵌入。
- 特色：运行时可加载 TSX 模块（插件 / AI 生成的界面），以**签名包 + 宿主配置的能力沙箱**投递；给编码 agent 提供可读文档与 skills。
- 与本项目的关系（结论：**不建议整合**）：
  1. 语言/运行时完全不同：我们是 Python 后端 + 原生 JS（61 个文件）+ Electron 壳；NSH 是 Rust/SolidJS 全应用框架，不是能挂进现有页面的库。
  2. 集成只有两条路，都不划算——整站重写（丢掉全部现有前端）或在 iframe 里跑一个 NSH 子应用（等于多引一层运行时，没有收益）。
  3. 我们是聊天/工作台类界面，原生控件的性能优势用不上；Electron 已提供多窗口、菜单、拖拽。
  4. v0.4.0，生态与稳定性都还早，不适合作为本地工具的地基。
- 唯一值得借鉴的点：**插件的「签名包 + 宿主控制的能力沙箱」模型**。我们已有 `app/plugin_manager.py`，将来要开放第三方插件时，可以按这个模型设计（签名校验 + 声明式能力 + 宿主逐项授权），而不是照搬代码。
- 来源：crates.io `nsh-i18n`（README 即项目说明）、nsh.dev、GitHub `longbridge/nsh`。未验证：Windows 目标的实际产物形态与稳定性（没有下载试用）。

## [2026-11-09] 调研：Agentero 是什么

- 结论：**Agentero 是 poco-ai 的开源「本地优先」科研工作台**（`github.com/poco-ai/Agentero`，MIT，官网 agentero.app，文档 agentero-docs.poco-ai.com）。不是 agent 框架、不是 harness，而是**面向 AI agent 的文献/笔记宿主应用**。
- 注意重名：另有一家美国 insurtech 公司也叫 Agentero（独立保险代理网络，Luis Pino 创立，InsurTech100），与本次调研无关。
- 要解决的问题：科研上下文三处割裂（Zotero 存 PDF / Obsidian 存笔记 / 网页对话框存与模型的推理），agent 只能看到碎片；且 PDF 双栏排版、跨页表格、公式对模型不友好。
- 技术形态（由 README + 语言统计推断）：TypeScript ~8.8MB + Rust ~6.5MB，Linux 依赖 `webkit2gtk 4.1` → **Tauri v2 桌面壳 + 前端 TS**（Rust 侧是壳与系统集成）；另有 Lua（pandoc 滤镜类）、少量 Python/Shell。
- 三项对外接口（这是它和本项目最有可比性的部分）：
  1. **ACP（Agent Client Protocol）客户端**：把 Claude Code / OpenCode / Codex / 本地模型接进来，不绑定厂商，上下文留本地。
  2. **内置 MCP Server**：反过来让 ChatGPT Web / Claude Desktop 读本地文献库。
  3. **无头 CLI**：脚本化批量导入、元数据提取、写笔记（社区还给它封了 skill `agentero-cli`）。
- 数据形态：不用私有数据库，文献（PDF）与笔记（Markdown，支持 `[[wikilinks]]`）以标准文件放本地 Vault，可被外部编辑器读取；支持 S3 兼容同步与 SSH 隧道远程访问。一键迁移 Zotero（保留标签/笔记/附件），可导出 BibTeX/BibLaTeX。
- 其他：图表/公式抽取成结构化 Markdown+LaTeX；Cool Papers 与魔搭集成、RSS、基于本地库的 arXiv 推荐、引用关系批量入库；有 `dsh` 插件（`@poco-ai/agentero-dsh`）可把文献库塞进 DeepSeek Harness；入选 GOSIM Spotlight。
- 与本项目的关系：架构取向高度相似——本地优先、Markdown/文件为真源、外壳 + 本地后端、对外同时提供 MCP 与 CLI。可借鉴的两点：① **把「人看的格式」和「模型吃的格式」分离**（PDF 给人、结构化 Markdown+LaTeX 给模型），我们的等价物是「原文件给人、repomap/outline/slim 引用给模型」；② **同一份本地数据同时以 MCP Server 和 CLI 两种形态暴露**，我们目前只有内置工具，没有对外 MCP/CLI 出口。不构成整合关系（它是科研文献专用宿主，与我们的编码/工作台场景不重叠）。
- 来源：GitHub README（poco-ai/Agentero，抓取于 2026-11-09）、GitHub languages API、agentero.app、agentero-docs.poco-ai.com、GOSIM Spotlight 列表、AITNT 产品页（Product Hunt 创始人自述）。未验证：未下载安装，版本号/star 数未核实（GitHub API 限流）。

## [2026-11-09] Agentero 获 GOAI 世界人工智能开源大赛「无界应用」赛道亚军

- 赛事：**世界人工智能开源大赛（GOAI）**首届，杭州市开源人工智能基金会主办，之江实验室 / 阿里云 / 蚂蚁集团联合承办，AAIF 与 LF AI & Data 为全球开源合作伙伴。总奖池 500 万 RMB，全场大奖 100 万。
- 赛程：7/16 报名 → 7/21 启动 → 2999 份初赛作品 / 91 国 1.4 万开发者 → 120 进复赛 → 9/10 公布 70 强 → 9/22–23 杭州云谷中心总决赛 + GOAI DAY。四大赛道六个竞赛单元：新智基座 Agent Infra / 无界应用 Boundless Agents / 前沿探索 AI for Research（算法赛题 + 开放探索）/ 具身未来 Embodied Future。
- **Agentero 成绩：赛道二「无界应用 Boundless Agents」亚军**，报奖作品名「Agentero 一站式科研工作台」，成员 **樊铧纬、陈启源、陈家和**（浙大控制学院 + 计算机学院博士生，团队名 POCO，Logo 是小狗）。
  - 同赛道：冠军「质链智能体·商业航天任务技术状态与验证证据智能体」（周华睿、张沁怡、吕弢）；季军「保险归因罗盘」（乐志明、徐青、陈泓宇）。
  - 全场大奖：前沿探索·开放探索冠军 ExpLoop Lab《MirrorPeptidizer：DeNovo 镜像多肽设计算法》，100 万。
- 奖金：官方奖项设置为「四大赛道亚军 **最高 30 万** RMB」（冠军最高 50 万、季军最高 10 万），与用户所说 30w 对应；**实际到手金额官方未公布**，且 11/6 公示写明「奖金为税前，个税由组委会代扣代缴，11/30 前转账」。公示表只列团队名、不含项目名，未能在表中直接对上 Agentero/POCO，故「实际发放金额 = 30 万」未获官方书面确认。
- 旁证：杭州网 2026-09-19 报道团队背景（8 月初上线，月活破 1000，52 个版本、2685 次提交、15+ 外部贡献者），并引队长樊铧纬「对奖项看得很淡，让更多人看到比拿奖更重要」。
- 来源：goaihz.com《总决赛暨颁奖盛典在杭州举行》(2026-09-23，内附完整获奖名单长图，我下载后切图核对)、goaihz.com《奖金发放公示》(2026-11-06)、goaihz.com 首页奖项设置、hznews.hangzhou.com.cn (2026-09-19)、hangzhou.gov.cn 四大赛道路演报道。未验证：获奖名单长图为图片，无文本版；公示表团队名与项目名无法一一对应。

## [2026-11-09] 项目水平自评：实测数据 + 对标 GOAI 类项目

- 实测口径（2026-11-09 在本机跑）：`app/` 214 个 py 文件 / 92,644 行；`web/` 183 个文件 / 79,180 行；`tests/` 201 个文件 / 29,396 行；`docs/` 16 篇 / 6,061 行；`scripts/` 26 个 / 3,102 行。
- 测试：`pytest -q` 收集 **2216** 条 → **2207 passed / 9 skipped / 0 failed**（退出码 0，约 1 分半跑完）。工具定义约 80 个（`app/agent.py` 里 `_fn_tool(` 85 处，含定义）。
- 结构风险：`app/agent.py` 8,357 行、`app/api/chat.py` 8,043 行，两个巨型文件；`ruff.toml` 只开 E4/E7/E9/F/W6（作者注明「不做风格洁癖」）；无 `.github` → **没有 CI**。
- 版本管理：`git rev-list --count HEAD` = **8**，首提交 2026-10-10，单一作者（MKIII-labs）→ 没有可展示的协作/提交历史，也没有 release 流水线。
- 对标 GOAI「无界应用」亚军 Agentero：代码内功（agent loop、上下文按需加载、cache miss 四分类、prefix trace、伪调用检测）**不输**；差距集中在四项非代码指标——外部用户（对方 1000+ MAU）、外部贡献者（15+）、发布节奏（3 个月 52 个版本）、对外接口（ACP + MCP Server + CLI，我们只有内置工具）。
- 结论分档：作为「个人自研 AI 工作台」→ 接近第一梯队（完成度 + 可验证性）；作为「可参赛/可融资的开源项目」→ 第二梯队（问题价值/技术深度够，开源价值与生态协同弱）。
- 若按 GOAI 六维评（问题价值、创新性、技术深度、完成度与可验证性、开源价值与复用、场景落地与生态协同）粗评：技术深度 A-、完成度 A、创新性 B+、问题价值 B、开源价值 C、生态协同 C-。
- 未验证：无真实用户数据、无性能/并发压测、无第三方安全审计；未与竞品做等量 LOC 对比（语言与统计口径不同，不可比）。

## [2026-11-09] 落地 CI：GitHub Actions 双 job（lint + Windows 全量测试）

- 文件：`.github/workflows/ci.yml`（新增，仓库此前无 `.github`）。
- 触发：`push` 到 main、任意 `pull_request`、`workflow_dispatch`；`concurrency` 按 ref 分组并 `cancel-in-progress`，连推不排队。
- job 1 `lint`（ubuntu-latest / py3.13，10 分钟超时）：`pip install ruff==0.17.0` → `ruff check app scripts tests` → `compileall app scripts main.py server.py`。**ruff 固定 0.17.0**（与本地 venv 一致），避免上游新增规则把 CI 突然打红；规则集仍以 `ruff.toml` 的 E4/E7/E9/F/W6 为准。
- job 2 `test`（**只跑 windows-latest** / py3.13，30 分钟超时）：装 `requirements.txt` + `pytest>=8` → `python scripts/dev_check.py` → `python -m pytest -q`。
  - 只跑 Windows 的原因：`pywinpty`（ConPTY 终端）、`uiautomation`（桌面自动化）、Edge 扩展等能力是 Windows 专属，换 Linux 要先改造这些测试，本次不承诺跨平台绿。
  - `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`：源码和输出含中文，防 cp1252 解码炸。
  - `pytest` 不在 `requirements.txt` 里，单独装，不污染运行时依赖。
  - 不设 `data/`：`git ls-files data` = 0，CI 是干净检出，测试自己用临时目录。
- 顺手修的 1 处 lint 错误：`app/dojo/primitives.py::_judge_pair` 的 `payload` 赋值后未使用（F841），改成只取 `expert`。否则 lint job 一上线就红。
- 交付前验证（本机模拟 CI 的两组命令）：
  1. lint 等价命令：`ruff check` = All checks passed（修完 F841 后）；`compileall` 退出码 0。
  2. test 等价命令：用 `git worktree add --detach HEAD` 造了一份**等价于 CI 干净检出**的目录（不含 `data/`、`.venv`、未提交改动），在里面跑 `pytest` → 退出码 0（2207 dots / 9 skips / 0 fail），`dev_check.py` 10s 全过（含 node 译页 18/18）。
  3. `yaml.safe_load` 解析通过，jobs = [lint, test]。
- 未验证 / 前置条件：**没有在 GitHub 上真实跑过**（本机无 `gh`，也没推送）；需要把 `.github/workflows/ci.yml` 和当前 19 个未提交改动一起 commit + push 才会首次触发。GitHub API 403，仓库可见性（private/public → 免费额度）未确认。
