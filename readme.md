# Galgame_monster

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows&logoColor=white)
![Stack](https://img.shields.io/badge/Stack-Vanilla%20JS%20%2B%20Python-000000)
![LLM](https://img.shields.io/badge/LLM-OpenAI%20Compatible-412991)
![Version](https://img.shields.io/badge/Version-3.4-2ea44f)

> **本机运行的 AI 工作台**：对话、文件 Agent、VS Code 风格 IDE、文档阅读与翻译、长期记忆、日程任务与功能大厅集中在同一个界面。程序在本地运行，模型走你自己的 API，数据全部留在 `data/`。

**3 秒速览**

| 你关心的 | 结论 |
| --- | --- |
| 是什么 | Windows 桌面端 AI 工作台：本地 Python 服务 + 原生 Web 界面，无账号、无云端同步 |
| 能做什么 | 在同一个窗口完成「对话 → 读写文件 → 跑命令 → 读论文 / 翻译 → 记入知识库 / 笔记 → 排进日程」 |
| 数据在哪 | 全部在项目内 `data/`；`config.json` 以明文保存 API 密钥 |
| 需要什么 | Windows + Python 3.11+ + 一个 OpenAI 兼容（或 Anthropic）模型接口 |
| 怎么启动 | `python -m venv .venv` → `pip install -r requirements.txt` → `python main.py` |

---

## 目录

- [1. 项目简介](#1-项目简介)
- [2. 核心功能](#2-核心功能)
- [3. 快速开始](#3-快速开始)
- [4. 使用说明](#4-使用说明)
- [5. 技术栈](#5-技术栈)
- [6. 配置说明](#6-配置说明)
- [7. 安全与边界](#7-安全与边界)
- [8. 数据、更新与备份](#8-数据更新与备份)
- [9. 已知限制](#9-已知限制)
- [10. 项目结构](#10-项目结构)
- [11. 常见问题](#11-常见问题)
- [12. 许可证](#12-许可证)

---

## 1. 项目简介

### 定位

Galgame_monster 是一个**纯本地**的个人 AI 工作台：界面用原生 Web 技术渲染，由本地 Python 服务提供后端，以应用模式窗口打开。模型调用走任意 OpenAI 兼容接口（也可用 Anthropic 原生协议）；没有账号体系，也没有云端同步。

与网页版 AI 的主要差异：

- **持续记忆**：用户画像、知识库图谱、笔记库与会话记忆跨会话复用，不必每次重新交代背景。
- **真实文件操作**：文件 Agent 有沙箱边界、写操作审批、逐次变更 diff 与步级撤回。
- **本地事件**：系统通知、定时任务、工作流与主动消息都在本机执行。

### 适用与不适用

**适合**

- 希望数据留在本机、并愿意自行配置 API Key 的个人开发者或学生。
- 需要 AI 直接读写本地项目文件，但希望每一步都可审阅、可撤销的场景。
- 需要长期记忆（画像 / 知识库 / 笔记）而不是反复重述背景的使用者。

**不适合**

- 需要多人协作、云端同步，或希望开箱即用、不做任何配置的场景。
- 非 Windows 环境（系统通知、计划任务与部分兼容性检测按 Windows 实现）。

---

## 2. 核心功能

| 模块 | 能力 | 关键点 |
| --- | --- | --- |
| Solo 对话 | 多会话、流式输出、停止 / 继续、上下文压缩、token 用量核算、轨迹回放 | 支持 Markdown / KaTeX、附件、联网搜索、语音输入与旁注提问 |
| 文件 Agent | 沙箱内读写与执行、计划驱动多轮、子代理、变更审阅与撤回 | 写操作按审批档位执行；沙箱外访问需显式开启且只读 |
| IDE 工作区 | Monaco 编辑器、文件树、多标签、终端、Git、Python 调试、语义索引、AI Tab 补全 | 内置 Monaco，离线可用；编码按原文件自动识别 |
| 文档阅读与翻译 | PDF / Word / PPT 沉浸式阅读、按章节翻译、术语表、公式与图表裁图、译文导出 | 左栏原页 + 右栏重排阅读流；导出仅译文 / 双语 PDF / Markdown |
| 记忆与知识 | 用户画像、知识图谱、会话记忆、笔记库、统一记忆索引 | 知识图谱可视化编辑；笔记为普通 Markdown 文件 |
| 检索与多模态 | 联网搜索、B 站视频 / 评论 / 字幕、网页抓取、图像识别、文档解析、语音输入与朗读 | 搜索默认关闭，可在设置中启用免注册引擎链或指定服务商 |
| 图片生成 | 通过本机 ComfyUI + FLUX 工作流出图，结果登记为工作台产物 | 默认关闭，不影响其它功能 |
| GAL 剧场 | 角色 / 立绘 / 剧本 / 配音 / 舞台状态，独立会话 | 逐段对白、打字机、选项、掷骰、关键词特效 |
| 功能大厅 | 图寻、数学竞技场、模拟股市、模拟面试、研究道场、学习观测、GAL 剧场 | 各模块自行注册；数据独立存储 |
| 工作台 | 日程、任务拆解与执行、产物归档、工作流、自动化看板、专注计时 | 任务与步骤实时落盘，重启可恢复 |
| 插件与 MCP | Python 工具插件、MCP 服务器、内置安装源与 DSH 索引 | 默认关闭；安装 / 启用需确认，重启后加载 |
| 可观测 | 轨迹面板记录系统提示 / 上下文 / 思考 / 工具 / 耗时；旁注提问不打断主对话 | 只读旁路，不进入模型上下文 |

---

## 3. 快速开始

### 3.1 环境要求

- **Windows**（系统通知、计划任务与部分兼容性检测按 Windows 实现）。
- **Python 3.11+**（推荐 3.12 / 3.13；`pywinpty` 要求 ≥3.10，依赖解析出的最新版 `matplotlib` 要求 ≥3.11）。
- **浏览器**：优先 Edge / Chrome。界面使用 `color-mix()` / `dvh` / `:focus-visible`，建议 Chromium 111+。
- **可选**：`desktop/` 下的 Electron 窗口壳（需 `npm install`）；未就绪时自动回退到 Edge / Chrome 的 `--app` 应用窗口。

### 3.2 安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> 安装与运行必须使用同一个解释器。`Galgame_monster.bat` 优先使用 `.venv\Scripts\pythonw.exe`；若依赖装在全局 Python，双击 bat 后终端、语音等功能会提示缺库。

### 3.3 启动

```powershell
.\.venv\Scripts\python.exe main.py
```

也可直接双击 `Galgame_monster.bat`（无控制台窗口）。首次启动后进入「设置 → 模型接入」填写 API 密钥。

- 退出：关闭应用窗口，或在控制台按 `Ctrl+C`。
- 单实例：已有实例在运行时再次启动只会打开已有窗口；检测到代码已更新时会提示先关闭旧实例。
- 端口：优复用上次端口（记录在 `data/app_port.txt`），避免 `localStorage` 数据因端口变化而丢失。

### 3.4 可选模型

语音识别在本机离线运行，模型放在 `data/models/`：

```powershell
python scripts/fetch_speech_model.py streaming   # 流式识别（语音输入默认使用）
python scripts/fetch_speech_model.py final       # 离线精修（两遍识别，可选）
python scripts/fetch_speech_model.py offline     # 离线整段识别（可选）
python scripts/fetch_speech_model.py denoise     # 流式降噪（约 8.4MB，可选）
```

代码语义检索需要本地 embedding 模型：

```powershell
python scripts/fetch_embed_model.py
```

网络受限时可先设代理：`$env:HTTPS_PROXY="http://127.0.0.1:7897"`。

### 3.5 桌面快捷方式（可选）

**方式一：用批处理文件建（推荐）**

1. 右键 `Galgame_monster.bat` → 发送到 → 桌面快捷方式。
2. 重命名后右键 → 属性 → 更改图标 → 选择项目根目录的 `app.ico`。

`.bat` 用 `%~dp0` 自定位，项目整体移动后快捷方式仍然可用。

**方式二：直接指向 pythonw**

1. 桌面右键 → 新建 → 快捷方式。
2. 目标填 `"<项目目录>\.venv\Scripts\pythonw.exe" "<项目目录>\main.py"`。
3. 「起始位置」可留空，`main.py` 用 `__file__` 定位项目根目录。
4. 图标改为 `app.ico`。

首次运行会在开始菜单生成 `Galgame_monster.lnk`；固定到任务栏才能获得正确的任务栏图标（依赖 AppUserModelID）。开机自启可把快捷方式放进 `shell:startup`。

---

## 4. 使用说明

侧栏有四个主模式：**Solo 对话**、**IDE 工作区**、**功能大厅**、**工作台**；另有独立的**插件**页与**轨迹**入口。

### 4.1 Solo 对话

| 功能 | 说明 |
| --- | --- |
| 流式输出 | 回复逐字显示，可随时「停止」（联网搜索阶段也能立即停）；被中断的回复可「继续输出」，写回同一条消息 |
| Markdown / 公式 | 标题、粗体、列表、引用、表格、代码高亮、KaTeX 公式；渲染失败退回原文，不显示红色报错 |
| 多会话 | 新建 / 切换 / 双击重命名 / 删除（二次确认）；会话列表支持置顶、分组与搜索 |
| 分支存档 | 每轮结束自动留档，也可手动打点命名；可从存档点「回退」或「分支」。每会话最多 60 个存档点 |
| 消息操作 | 用户消息可「撤回」「修改」后重发；AI 回复可「重新发言」、切换源码视图；支持复制 / 删除 |
| 提问标记 | 给关键提问起名，Solo 模式右侧出现悬浮按钮，一键跳回；右键改名 / 删除 |
| 旁注提问 | 流式输出时划选一段文字就地提问，走独立接口，不写会话、不打断主对话 |
| 输入框增强 | `/` 指令（`/clear` `/new` `/compress` `/kb` `/settings` `/theme` `/plan` `/polish` `/spec` `/shorten` `/expand` `/translate`）、`@` 引用记忆条目、历史补全、`Ctrl/⌘+Enter` 发送、`Ctrl/⌘+I` AI 优化输入 |
| 附件 | 回形针 / `Ctrl+V` / 拖入窗口；长文本粘贴收成轻量文本块；支持 txt / md / tex / csv / json / yaml / 代码 / docx / xlsx / pdf |
| 联网搜索 | 默认关闭，可在设置里启用；自动判断是否需要联网，过程可见，完成后列出来源并标注 `[1] [2]`；`#web` 强制联网，`#web <URL>` 直接读页 |
| 语音输入 | 边说边出字（本机流式识别），停止后可用精修模型复识别，可选 AI 矫正；降噪可单独开启 |
| Token 用量 | 7 天 / 30 天 / 12 个月概览、按模型趋势、输入输出堆叠、按端点 / 通道 / 来源明细与逐次溯源 |
| 上下文上限 | 输入框上方实时显示占用进度条；接近上限自动压缩较早历史 |
| 子代理 | 「设置 → 子代理」用 Markdown 定义工具白名单、模型、轮次与只读权限；主 Agent 可 delegate / explore / followup |
| 轨迹 | 侧栏「轨迹」查看模型实际收到的系统提示、上下文、思考、工具与每步耗时，可按工具过滤、搜索、导出 |
| 主题与键盘 | 界面主题 6 套；侧栏会话、思考卡、工具卡、笔记树均可 `Tab` 走通，危险操作二次确认 |

### 4.2 文件 Agent（沙箱）

| 项目 | 说明 |
| --- | --- |
| 开启与沙箱 | 点输入区上方「Agent」，选择沙箱目录；设置按会话保存 |
| 工具分组 | **读取** list / file / files / search / semantic / map；**编辑** write / patch / mkdir / move / copy / delete；**执行** exec / terminal；**代码** diagnostics / definition / references / hover / symbols / implementation / calls / rename / check / find / impact / context；**Git** status / diff / log / commit / split；**计划** update / patch；**子代理** delegate / explore / followup / status / result / cancel；**联网** search / fetch / note；**历史** list / undo / redo / recover；**视觉** render / qa / views / geometry / scene；**图片** see / compare / analyze；**素材** scan / convert / atlas / inline / validate / mesh；**音频** probe / compare；**工作流** run / script / status / resume / cancel；**其他** sleep / reminder，以及 `calendar.*` / `task.*` / `memory.*` / `note.*` / `bili.*` / `stock.*` |
| 安全边界 | 只接受沙箱内路径；`.git` / `.venv` / `node_modules` / `__pycache__` 与本项目 `data/` 永久禁止访问；「读沙箱外」需明确开启且只读 |
| 审批档位 | `ask` 手动审批 / `ai` 智能审批（默认；只读与普通命令放行，安装、联网、改历史类才问）/ `full` 完全自动 / `custom` 自定义规则（沙箱内 `.gm/agent-rules.json`） |
| Shell 自由度 | `standard`（默认，允许 `\|` 与沙箱内重定向，每段仍过白名单）/ `strict`（禁用管道与重定向）/ `trusted`（额外允许 `;` `&&` `\|\|`） |
| 变更审阅 | 变更卡显示文件名、完整路径、增删行数；支持单栏 inline diff、全部撤销 / 全部保留；`Ctrl+Backspace` / `Ctrl+Enter` |
| 撤回 | 每次写操作生成工具卡片，点「撤回」恢复快照；默认保留最近 60 条往期写入（`agent_max_undo`，0 = 不限），仅存内存，重启失效 |
| 中途提问 | 需要决策时弹提问卡：可多问、可翻页、可留空；选项支持说明与「推荐」标记，题面支持 Markdown；可点「让 AI 自己决定」或停止来推进，历史卡可「重新提问」 |
| 收尾判定 | 模型调用 `finish` 声明完成，或本轮不再调用工具；计划仍有未完成步骤时自动续跑 |
| 规划模式（`/plan`） | 本轮只读：只下发读取 / 搜索 / 提问与计划工具，产出「目标 + 完成判据 + 里程碑 + 首步拆解」，确认后再执行 |
| 验收 | finish 前由独立验收子代理按实际产物核对打分（默认阈值 70），没有证据的「通过」不算通过 |

### 4.3 IDE 工作区

VS Code 风格：活动栏、文件树、多标签编辑器、状态栏，内置 Monaco（离线可用）。

| 功能 | 说明 |
| --- | --- |
| 基础编辑 | `Ctrl+S` 保存、`Ctrl+P` 快速打开、`Ctrl+Shift+P` 命令面板；沙箱内搜索；UTF-8 / GBK 自动识别并按原编码写回 |
| 布局 | 文件管理区可收起（`Ctrl+B`）；右侧聊天栏可拖拽调宽，双击还原 |
| 主题 | 跟随界面主题 / 跟随 VS Code / Dark Modern / Light Modern / Dark+ / Light+ / Trae Dark / Trae Light / Trae Deep Blue |
| 空白字符 | 关闭 / 仅选区内 / 仅行尾 / 词之间 / 全部 |
| 侧边栏 | 资源管理器 / 沙箱内搜索 / 源代码管理 / 运行和调试 / 批注 / 文献库 |
| 终端 | 内置终端面板（`Ctrl+\``） |
| 调试 | Python 调试（debugpy）：断点、单步、变量与调用栈；未安装时给出安装提示 |
| 语义索引 | 定义 / 引用 / 实现 / 调用链 / 符号搜索 / 语义重命名 / 影响面分析 |
| AI Tab 补全 | 光标停顿 350ms 后给出灰色幽灵建议：`Tab` 接受、`Esc` 关闭；行尾补全，最多 8 行；需要配置 `light_model` |
| 外部编辑 | 一键在本机 VS Code 中打开当前沙箱；可只读导入 VS Code 设置与扩展清单 |
| 文档阅读 | 双击 PDF / Word / PPT 进入沉浸式阅读器（见 4.4）；`@文件` 支持 PDF（含 `#页码`）与 docx / xlsx / pptx 正文注入 |

### 4.4 文档阅读与翻译

左侧是 100% 原页，右侧是按段落重排的阅读流。默认只看原页；点「译本页 / 译全文」才启动翻译。

| 能力 | 说明 |
| --- | --- |
| 版面重建 | 逐字符 bbox → 行段 → 双栏判定 → 段落合并；跨页续段自动合并；标题按相对字号分级 |
| 结构感知 | 列表按原页左边界识别缩进；定理 / 引理 / 证明加左侧竖线分组；图注 / 表注自动弱化 |
| 公式与图表 | 行间公式 / 多行公式组直接裁原页；配置视觉模型后可识别为 LaTeX 用 KaTeX 重排（失败保留原图）；行内公式用占位符保护 |
| 翻译内核 | 同章节连续段落成批（≤4 段 / 1600 字），注入章节名、上一段原文与术语表；只补缺失段，支持断点续译与三级重试；并发默认 3（`reader_translate_parallel`，1–4） |
| 术语表 | 首次翻译自动抽取高频术语并统一译名；设置里可逐条编辑、保存或重新生成 |
| 阅读辅助 | 划词翻译 / 提问 / 高亮 / 摘录；批注抽屉支持按页跳转、复制引用、导出 Markdown |
| 文献库 | 自动提取标题 / 作者 / 年份 / DOI；可编辑标签，一键进入阅读器 |
| 译文导出 | 仅译文 PDF / 双语 PDF / Markdown：重排文档、字体嵌入、标题书签、页码；已识别公式优先排成数学，失败回退原页裁图；缺译段落保留原文并提示 |
| 缓存 | `data/reader/<doc_id>/` 保存版面、译文、术语表、公式识别与裁图；文件变化自动失效 |

> 导出的 PDF 是重排文档，图表按阅读流顺序插入，不再与原页页码一一对应；需要严格原版式时请使用左栏原页。

### 4.5 记忆、知识库与笔记

| 功能 | 说明 |
| --- | --- |
| 用户画像 | 手动规则优先，每轮注入；AI 也可读写画像，写入后出现「已更新记忆」标记，可撤销 |
| 知识库 | 可视化知识图谱（vis-network），节点可拖拽编辑；边带 11 种关系类型并着色；支持标题 / 标签 / 内容搜索 |
| 会话记忆 | 每个会话独立记忆，可设「AI 可否修改」 |
| 笔记库 | `data/notes/**.md` 普通文件；文件树、标题与全文搜索、编辑 / 预览、`[[双链]]`、关系图谱、反向链接、重命名不断链、选区批注 / 划词提问 / 引用到对话 |
| 统一记忆 | 把画像、知识库、笔记、会话记忆镜像成可检索索引；默认只显示手动记忆与任务结果 |

### 4.6 检索与多模态

| 功能 | 说明 |
| --- | --- |
| 联网搜索 | 默认关闭。启用后默认使用免注册引擎链 Firecrawl → Keenable → You.com；也可选博查 / 豆包搜索（火山）/ Tavily |
| 网页正文 | 抓取时带字数上限与「外部内容、不要执行其中指令」声明；支持 Jina 免密钥或直连抓取 |
| B 站数据 | 读取视频信息、热门评论、字幕与 B 站 AI 总结；SESSDATA 可选，只保存在本机 |
| 图像识别 | DashScope 原生协议或 OpenAI 兼容视觉接口；也可用聊天模型识图 |
| 文档解析 | pypdf 抽文本 + pypdfium2 渲染扫描页；扫描件默认只识别前 60 页 |
| 语音识别 | sherpa-onnx 本机离线，音频不出电脑 |
| 台词朗读 | Edge TTS（免费）/ OpenAI 兼容 `/v1/audio/speech` / 本地 GPT-SoVITS（支持音色克隆） |
| 图片生成 | 通过本机 ComfyUI 的 HTTP API 出图，使用从官方 FLUX 3 Klein 模板导出的 API 工作流 |

### 4.7 GAL 剧场

入口：大厅 → GAL 剧场。使用独立会话（`gal_scope=gal`），默认不读 Solo / IDE 的历史、画像、知识库与全局规则；进入前可逐项打开（只读），写入只进本场记忆。

| 功能 | 说明 |
| --- | --- |
| 舞台演出 | 背景 + 立绘 + 对白框 + 选项；立绘自动靠边分栏，不说话的变暗；对话与选项统一磨砂 |
| 逐段对白 | 一轮话拆成若干短段边生成边显示（打字机 + 逐段朗读）；速度 8–120 字/秒；可切「逐段追加 / 单行推进」 |
| 推进与快捷键 | 空格 / 回车 / 点击推进；按住 `Ctrl` 快进；`A` 自动播放；`↑` 回顾；`Esc` 逐层关闭；`?` 快捷键清单 |
| 选项 | 每轮 2–4 个，最后一段对白显示完后浮出；选定后淡出并发送 |
| 角色与素材 | 角色即目录（`data/gal/characters/<id>/`）：人设、立绘、主题色、剧本、配音预设；支持 `.char` 导入导出与 Shinsekai 角色包 |
| 台词朗读 / 音色克隆 | Edge TTS / OpenAI 兼容 / 本地 GPT-SoVITS；`⋯ → 快速克隆` 用 5–10 秒参考音频克隆音色 |
| AI 自动写剧本 | 依据角色设定 + 世界观 + 用户画像生成分幕大纲，再逐幕展开 |
| 舞台状态与档案 | AI 自建状态参数（好感度 / 危险度 / 时间 / 地点…），撤回时回滚；剧情档案支持列表与关系图、俯视方位图 |
| 随机判定 | 结果不确定时 AI 可声明掷骰（`d20+调整值 vs DC` 或自由骰），由系统掷出并回灌 |
| 关键词特效 | 台词里写 `*闪光*` / `*震动*` / `*黑屏*` / `*淡入*` 触发动画与音效 |

### 4.8 功能大厅

卡片式入口，由各模块自行注册，当前 7 个：

| 功能 | 说明 |
| --- | --- |
| GAL 剧场 | 见 4.7，作为大厅入口进入独立演出会话 |
| 图寻游戏 | 你与 AI 对手在地图上标拍摄位置，距离近者赢；题库来自 Panoramax 开放街景 |
| 数学竞技场 | 题库（数学分析 / 高等代数等）与竞技 / 复习记录 |
| 模拟股市 | 独立 SQLite 行情与学生交易模拟：K 线、市价 / 限价单、部分成交、公司行为、绩效指标与 Markdown / CSV 报告导出 |
| 模拟面试 | 简历解析、JD 匹配、分轮追问与报告 |
| 研究道场 | 研究型任务（复刻 / 还原 / 从零实现）的多阶段练习与评估 |
| 学习观测 | 汇总对话、数学竞技场、模拟卷、研究道场留下的学习行为，可按学科与时间范围查看 |

> 图寻底图使用 OpenStreetMap 瓦片（WGS-84）。不能换成高德 / 腾讯瓦片：GCJ-02 偏移会让距离系统性偏出几百米。

### 4.9 工作台

顶部可在 **今天 / 周 / 月 / 看板 / 统计** 五种视图之间切换；「今天」仍是日程、任务、产物三栏。

| 能力 | 说明 |
| --- | --- |
| 今天 | 今日 / 未来 7 天日程、任务列表、产物三栏；支持自然语言「快速添加」日程 |
| 周视图 | 7 列时间轴：每天的日程、截止任务与专注分钟一屏展开 |
| 月视图 | 月历网格 + 选中日明细；可翻月、回到今天，日程与截止任务都显示在日格中 |
| 任务看板 | 按状态分列（待开始 / 进行中 / 已暂停 / 失败 / 已完成 / 已取消）；拖拽卡片改状态，同列拖动调整顺序 |
| 批量操作 | 看板勾选多个任务后批量完成 / 取消 / 删除 / 设优先级 |
| 任务字段 | 截止时间、优先级、标签、预计分钟、重复（每天 / 每周 / 每月）、父任务（子任务）、前置依赖（完成前不能开始） |
| 重复任务 | 勾选「标记完成」后，按重复规则自动生成下一轮任务与截止日程 |
| 专注计时 | 顶部「专注」：15 / 25 / 45 / 60 分钟，可暂停 / 继续 / 结束并记录；可关联任务，结束后进入统计 |
| 任务模板 | 任务栏「模板」：把常用流程存成模板，一键新建任务；任务详情里也可「存为模板」 |
| 统计 | 近 7 / 30 天：完成任务、新建任务、专注分钟、逾期数、每日柱状图、标签分布与逾期任务列表 |
| 个人日程 | 新建 / 删除，支持每天 / 每周 / 每月重复、地点、提前 5–60 分钟提醒；创建时做时间冲突检测 |
| 任务执行 | 对话里说「拆成任务并执行」，AI 用 `task.create` 生成步骤；也可手动建；支持暂停 / 继续 / 取消 |
| 写操作确认 | 写文件、建日程等写操作步骤第一次停在「待确认」，确认后才继续 |
| 断点恢复 | 任务与步骤实时落盘；进程重启后在跑的任务转为「已暂停」，不自动重跑 |
| 产物归档 | 任务写出的文件登记到产物栏，可在线预览文本 / 图片 |
| 工作流 | 触发器（手动 / 定时 / 事件）→ 动作（建任务、发通知、建日程、发消息、本机朗读、调用能力） |
| 自动化 | 自动化运行看板显示阶段、工具、验收进度；任务在服务端执行，可恢复 |
| 图片生成 | 调用本机 ComfyUI 生成图片并登记为产物 |
| 场景包 | 通用 / 办公 / 学习 / 生活四种场景，影响系统提示的场景纪律与能力展示顺序 |

### 4.10 插件与 MCP

侧栏插件按钮打开整屏「插件」页：

- **已安装**：Python 工具插件（启停 / 详情 / 卸载）与 MCP 服务器开关。
- **安装来源**：内置官方插件源（当前为空）、本地目录、zip、Git 仓库、单文件 `.py`。
- **发现**：内置官方插件与 DSH 生态索引（只读）。

> 插件是任意 Python 代码：安装与启用都要二次确认，且**重启应用后才加载**；总开关关闭时任何工具插件都不会加载。MCP 默认关闭。

### 4.11 设置与快捷键

设置面板左侧为 **4 组 15 分类**：

| 分组 | 分类 |
| --- | --- |
| 常用 | 模型接入、联网与数据源、上下文与压缩、主动与提醒 |
| Agent 与能力 | Agent 执行、子代理、Skill、图片生成 |
| 用户与外观 | 用户画像与学习、主题与外观、GAL 演出、语音与朗读 |
| 系统 | 快捷键、数据与高级、功能说明 |

- 顶部搜索框可直达设置项并高亮；`Esc` 关闭、`Ctrl+S` 保存、`←`/`→` 切换分类。
- 快捷键可逐项改键：同组撞键拒绝，跨组撞键允许并提示，用户自定义优先。
- 分组为 GAL 演出 / IDE 工作区 / 编辑器（VS Code）/ 输入框 / 全局。

---

## 5. 技术栈

| 层级 | 技术 |
| --- | --- |
| 界面 | HTML / CSS / JavaScript（无框架、无构建） |
| 窗口 | Electron 窗口壳（可选）/ Edge、Chrome 应用模式（`--app`） |
| 后端 | Python 标准库 `http.server`（多线程）+ `requests`；SSE 流式响应 |
| 前端第三方库 | KaTeX、Monaco、pdf.js、vis-network、xterm、Seti 图标（本地内置）；highlight.js、ECharts、Mermaid（CDN，带备用源） |
| 编辑器 | Monaco（本地内置，离线可用） |
| PDF 阅读 | pdf.js（本地渲染）· pypdfium2（裁图 / 扫描页）· pypdf（文本层） |
| PDF 导出 | reportlab（矢量文字、字体嵌入、书签）· matplotlib mathtext（公式，缺失时回退裁图） |
| 知识图谱 / 图表 | vis-network（本地内置）· ECharts · Mermaid |
| 地图 | Leaflet + OpenStreetMap 瓦片（WGS-84） |
| 图寻题库 | Panoramax 开放街景图库（免密钥） |
| 数据存储 | JSON + Markdown + SQLite（标准库 `sqlite3`，WAL）+ JSONL |
| 语音识别 | sherpa-onnx（本机离线） |
| 台词朗读 | Edge TTS / OpenAI 兼容 `/v1/audio/speech` / 本地 GPT-SoVITS |
| 代码补全 | Monaco InlineCompletionProvider + FIM 提示（走 `light_model`） |
| 语义检索 | 本地 ONNX embedding（fastembed） |
| 语言服务 | pyright（首次调用经 npx 拉取） |
| 调试 | debugpy（Python DAP） |
| 图标 | 内嵌 SVG sprite（`currentColor` 跟随主题） |

---

## 6. 配置说明

### 6.1 配置文件

- 唯一配置文件：`data/config.json`（首次启动自动生成，JSON 格式）。
- 设置页保存时把变更合并进当前配置后整体写回；文件里缺失的键由 `app/config.py` 的 `DEFAULT_CONFIG` 兜底。
- 密钥、预设、主题、功能开关全部在这个文件里；`data/` 已在 `.gitignore` 中忽略。

### 6.2 聊天模型

任意 OpenAI 兼容接口，填写地址、模型名与密钥：

| 服务 | 接口地址 | 模型名 |
| --- | --- | --- |
| DeepSeek 官方 | `https://api.deepseek.com/chat/completions` | `deepseek-flash` |
| 本地 Ollama | `http://localhost:11434/v1/chat/completions` | 你拉取的模型名 |

只填 base URL（如 `https://api.deepseek.com`）时会自动补全为 `/v1/chat/completions`。地址 + 模型 + 密钥可以打包成「API 预设」，随时切换服务商；也可用 `chat_fallback` 配置故障转移链（主服务商失败且未输出任何内容时按序尝试）。

主要参数（设置 → 模型接入）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `chat_kind` | `""` | 协议：留空按地址自动判断，可选 `openai` / `anthropic` |
| `chat_think_enabled` / `chat_think_level` | `false` / `high` | 是否启用深度思考及档位 |
| `chat_context_limit` | `1000000` | 上下文窗口 tokens；`0` = 完全不限 |
| `chat_reserve_output` | `16384` | 为输出预留的 token |
| `chat_max_tokens` | `8192` | 单次输出上限 |
| `chat_output_window_ratio` | `0.25` | 输出上限最多占窗口的比例 |
| `chat_retry_mode` | `always` | 断流重试策略（瞬时错误无上限重试，billing / auth / context 类不重试） |
| `chat_stream_idle_timeout` | `90` | 上游多久没有任何数据就中止本轮（秒） |
| `chat_stream_stall_timeout` | `45` | 有数据但无有效增量的超时（秒） |
| `chat_persist_reasoning` | `true` | 是否把思考落库（只写本地，不上传） |
| `usage_daily_tokens` | `0` | 每日 token 上限；`0` = 不限 |

### 6.3 轻量模型与视觉模型

`light_model` 用于摘要、记忆挑选、GAL 判定、IDE Tab 补全等短请求；可另填 `light_url` / `light_api_key`。留空时除 IDE 补全外的轻量任务沿用主模型。阅读器的翻译与术语表固定用主聊天模型（避免小模型术语漂移）。

视觉模型支持两种协议：

- DashScope 原生：图片放 `input.messages[].content`，结果取 `output.choices[0].message.content`。
- OpenAI 兼容：图片放 `messages[].content`（`type=image_url`），结果取 `choices[0].message.content`。

顶部「使用聊天模型识别图片」开关开启后，改用聊天模型识图（要求模型自带视觉）。

### 6.4 上下文与压缩

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `compress_ratio` | `0.8` | 占用达到窗口比例时后台预压缩 |
| `compress_keep_ratio` | `0.16` | 压缩时保留最近原文的比例 |
| `compress_summary_tokens` | `8192` | 摘要输出上限 |
| `history_working_set_tokens` | `40000` | 跨回合历史原文预算；`0` = 关闭（回到按窗口压缩） |
| `history_process_keep_turns` | `2` | 最近几条 assistant 消息保留原文 |
| `microcompact_enabled` | `true` | 对单条过大的工具结果做轻量摘要 |
| `session_memory_enabled` | `true` | 回合结束后合并会话工作记忆 |
| `token_profile` | `balanced` | 省费模式；`quality` 与旧行为逐字节一致 |

**压缩口径**：发给模型的可用预算 = `min(窗口 × compress_ratio, 窗口 − 输出上限 − 余量)`；工作集预算只影响历史原文，当前回合永远保留原文，模型仍可按需 `read_file` / `read_tool_result` 取回细节。

### 6.5 Agent 执行

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `agent_max_rounds` | `0` | 单轮最多「调工具 → 再问模型」几次；`0` = 不限 |
| `agent_auto_continue` | `3` | 任务没做完时自动续跑几次；`0` 关闭，`-1` 不限 |
| `agent_repeat_limit` | `5` | 同工具同参数连续几次就拦截 |
| `agent_cmd_timeout` | `60` | 命令默认超时（秒）；`0` = 不限 |
| `agent_require_read` | `true` | 覆盖已有文件前必须先读过 |
| `agent_prefetch_max` / `agent_prefetch_workers` | `24` / `10` | 只读工具并发预取上限 / 线程数 |
| `agent_tool_parallel` | `true` | 执行阶段连续只读调用按段并发 |
| `agent_explore_enabled` | `true` | 允许并行只读子代理 |
| `agent_acceptance_review` | `true` | finish 前跑验收子代理，`agent_acceptance_min` 默认 70 |
| `agent_plan_review` | `true` | 第一份计划先挂起等用户批准 |
| `agent_shell_mode` | `standard` | Shell 自由度：`strict` / `standard` / `trusted` |
| `agent_max_undo` | `60` | 每会话保留的往期写入快照数；`0` = 不限 |
| `tool_result_max` | `30000` | 单条工具结果回灌上限（超出按头尾裁剪） |
| `tool_spill_max` | `12000` | 超过该字符数的工具结果落盘，模型可用 `read_tool_result` 取回 |
| `subagent_context_tokens` | `60000` | 子代理独立上下文预算 |

### 6.6 联网搜索与 B 站

联网搜索默认关闭（`search_enabled = false`）。启用后默认使用「自动」免注册引擎链：

| 引擎 | 免注册额度 | 说明 |
| --- | --- | --- |
| Firecrawl（默认首选） | 每月 1000 credits | 搜索 + 云端抓页（能渲染 JS 页面） |
| Keenable | 每小时 1000 次 / IP | 仅搜索 |
| You.com | 每天 100 次 / IP | 仅搜索，部分网络不可达会跳过 |

也可改为单服务商：豆包搜索（火山）、博查 Bocha、Tavily。额度 / 鉴权失败的引擎会冷却一段时间。

相关参数：`search_provider`、`search_engine_keys`、`search_engine_enabled`、`search_count`（默认 8）、`search_fetch`、`search_fetch_cloud`、`search_fetch_count`（默认 4）、`search_fetch_chars`（默认 2500）、`search_merged_max`（默认 12）、`search_fetch_reader`（`jina` / `direct`）、`search_cache_ttl`（默认 3600 秒）。

B 站数据读取在「设置 → 联网与数据源 → B 站数据」配置：不填 SESSDATA 也能读视频信息与评论；字幕与官方 AI 总结需要登录凭证。SESSDATA 只保存在本机，不会发送给模型。

### 6.7 语音识别与台词朗读

语音识别本机离线，模型放在 `data/models/`；降噪与精修可选，未下载时点麦克风会提示。

| 朗读服务 | 地址 | 密钥 | 说明 |
| --- | --- | --- | --- |
| Edge TTS | 留空 | 不需要 | 免费免密钥；需能访问微软服务 |
| OpenAI 兼容 | 如 `https://api.openai.com` | 需要 | `POST {base}/v1/audio/speech` |
| 本地 GPT-SoVITS | 默认 `http://127.0.0.1:9880` | 不需要 | 可克隆音色 |

本机跑 GPT-SoVITS（用于音色克隆）：

```bash
python api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS/configs/tts_infer.yaml
```

硬件要求：推理需 6GB 显存起，微调 8–12GB。RTX 50 系必须用 cu128 版 PyTorch。

### 6.8 提醒与主动消息

调度线程每 15 秒扫描一次到期任务，任务存在 `data/tasks.json`。

- **定时提醒**：对话里说「明天 9 点提醒我」由 AI 登记，或在设置里手动添加。系统通知通过 Windows 计划任务实现，程序关闭也会提醒。
- **主动聊天**：仅程序运行时发生；间隔为最短–最长之间的随机值；3 分钟内有活动会先让路，连续 2 次无回应自动暂停。

### 6.9 图片生成

默认关闭（`image_enabled = false`）。需要本机运行 ComfyUI：在 ComfyUI 里打开官方 FLUX 3 Klein 模板，用「另存为 API 格式」导出工作流 JSON，把路径填进「设置 → 图片生成」。模型建议 INT8 / NVFP4 量化版 + Qwen3-VL-4B 编码器 + FLUX.2 VAE。

### 6.10 余额查询

- DeepSeek：按接口地址自动识别，调用 `GET /user/balance`。
- 其它厂商：在「余额查询地址」填 GET + Bearer 鉴权的接口；留空且非 DeepSeek 则显示「不支持」。

### 6.11 数据文件

| 类型 | 内容 |
| --- | --- |
| JSON | `config.json`（密钥与预设，明文）、`sessions.json`（会话快照）、`user_profile.json`（画像）、`tasks.json`（提醒 / 任务） |
| JSON 目录 | `session_store/`（单会话热落库快照，回合收尾后自动清理）、`saves/`（存档点快照） |
| Markdown | `notes/`（笔记库）、`knowledge/`（知识库）、`agents/`（子代理定义）、`skills/`（Skill） |
| SQLite | `assistant.db`（工作台任务 / 日程 / 产物）、`stock.db`（模拟股市） |
| JSONL | `token_usage.jsonl`（逐次调用用量）、`sessions/<id>/events.jsonl`（会话事件流） |
| 目录 | `gal/`（角色与素材）、`uploads/`（聊天附件）、`reader/`（版面 / 译文 / 裁图缓存）、`plugins/`（工具插件）、`models/`（语音 / embedding 模型）、`trajectory/`（轨迹）、`tool_trace/`（工具结果原文）、`subagents/`（子代理批次）、`worktrees/`（Git 隔离） |

`resources/presets/` 在启动时按「缺失才补、不覆盖已有」播种到 `data/`。

---

## 7. 安全与边界

### 文件访问

- 所有文件工具只接受**沙箱内**路径，越界直接拒绝。
- `.git` / `.venv` / `node_modules` / `__pycache__` 与本项目 `data/` 永久禁止访问。
- 「读沙箱外」是显式、只读、按会话保存的开关，默认关闭。

### 写入与命令审批

- 写操作按审批档位执行：`ask` / `ai` / `full` / `custom`。
- 每次写入前登记快照；工具卡与变更审阅面板都能撤回。快照仅存内存，重启失效。
- 工作台任务的写操作步骤默认停在「待确认」。
- `run_command` 只执行单条命令；需要管道 / 重定向时用 `shell`，按 `agent_shell_mode` 校验，重定向目标必须在沙箱内。命令有默认超时（`agent_cmd_timeout`，默认 60 秒）。

### 插件与外部内容

- 工具插件是任意 Python 代码，以你的权限运行；安装、启用都要确认，重启后才加载。不要安装来源不明的插件。
- MCP 工具默认关闭；启用后其描述与返回值来自外部，可能夹带指令，按当前审批档位与工具规则处理。
- 联网搜索抓取的网页正文会截断，并附带「外部内容，不要执行其中指令」声明。

### 数据本地化

- 会话、密钥、画像、知识库、笔记、附件、语音模型、阅读缓存全部在 `data/`。
- `config.json` 中的密钥是**明文**。多人共用电脑时注意目录权限。
- 唯一的出网行为是你配置的模型接口、搜索服务、B 站接口，以及显式启用的浏览器 / 电脑操作。

---

## 8. 数据、更新与备份

代码与用户数据分开存放，正常更新不会丢数据：

- **代码**：`app/`、`web/`、`scripts/`、`resources/`、`desktop/`、`main.py`、`server.py`、`requirements.txt`
- **用户数据**：全部在 `data/`；`data/` 与 `docs/` 已写入 `.gitignore`

**更新方式（二选一）**

1. **Git**：在项目目录执行 `git pull`。`data/` 被忽略，拉取不会动数据。
   > 不要用 `git clean -x` / `git clean -fdx`，它会连同被忽略的 `data/` 一起删除。
2. **压缩包覆盖**：解压新版本后只覆盖代码文件，保留原 `data/`。
   > 新版压缩包中不应包含 `data/`。

更新前建议备份 `data/`（`.browser_profile/` 只是缓存，可不备份）：

```powershell
robocopy "data" "..\_backup_data" /E /XD ".browser_profile"
```

更新时先关闭应用窗口再覆盖；若 `requirements.txt` 有变化，重新执行 `pip install -r requirements.txt`。

应用内也提供「备份与恢复」：导出 zip（含 `manifest.json` 与 SHA-256 校验）、恢复前预检并自动生成安全备份。

---

## 9. 已知限制

**平台与环境**

- 仅支持 Windows：系统通知、计划任务与部分兼容性检测按 Windows 实现。
- 程序关闭时，主动聊天、工作流、任务执行都不会发生；定时提醒除外（走计划任务）。
- 需要保持后端进程运行；关闭控制台或应用窗口会一并退出。

**能力边界**

- 变更快照只在内存中（默认最近 60 条），重启后旧改动无法再撤销。
- 任务写操作没有「全部自动执行」开关，这是有意的安全取舍。
- 工作台重复任务只支持每天 / 每周 / 每月；子任务只有一层父子关系，前置依赖只做「未完成则不能开始」的检查。
- 扫描件 PDF 默认只识别前 60 页。
- PDF 翻译导出生成的是重排文档，图表位置不再与原页一一对应；需要原版式时用左栏原页。
- 公式识别准确率取决于视觉模型；识别结果不合法时保留原截图，不会渲染成红色报错。
- 导出的 PDF 依赖 `reportlab`；未安装时只有 PDF 导出不可用。
- `run_command` 不支持管道、重定向与后台常驻；需要复杂命令时用 `shell`。
- IDE 的 AI Tab 补全依赖 `light_model`，且只对代码类语言、行尾位置生效。
- 代码语义检索需要先运行 `scripts/fetch_embed_model.py` 下载本地模型。
- 笔记库是 Obsidian 核心功能的自主实现，不含插件系统、主题、模板与同步；与 Obsidian 无代码关系。
- 图寻游戏必须使用 WGS-84 瓦片（OpenStreetMap）。
- 语音识别需要手动下载模型；未下载时麦克风不可用。
- 工具插件需要重启应用后才加载。

**非目标**

- 不做云端同步与多人协作。
- 不做云端插件交易与审核：插件页只提供本地安装与公开索引，来源需自行确认。
- 不承诺与 VS Code 插件生态兼容。

---

## 10. 项目结构

```
Galgame_monster/
├── main.py               # 启动器：起本地服务 + 打开应用窗口
├── server.py             # 兼容入口（转发到 app/httpd.py）
├── app/                  # 后端实现（按功能拆分）
│   ├── store.py          # 路径常量、共享锁、带 mtime 缓存的 JSON 读写、APP_VERSION
│   ├── httpd.py          # HTTP 入口：路由表 + 静态资源 + SSE（含常驻 /api/events）
│   ├── api/              # HTTP 接口实现，按域拆分
│   │   ├── chat.py       # 对话主循环：装配 / 流式 / 工具 / 收尾
│   │   ├── sessions.py   # 会话、消息、提问标记；savepoints.py 存档点
│   │   ├── sandbox.py    # 沙箱文件读写、变更审阅、IDE 补全接口
│   │   ├── reader.py     # 文档阅读：解析 / 裁图 / 翻译 / 术语表 / 导出
│   │   ├── plugins.py · market.py   # 工具插件与 DSH 索引
│   │   ├── trajectory.py · debugger.py · codeindex.py
│   │   ├── gal_*.py      # GAL：角色 / 剧本 / 媒体 / 配音
│   │   ├── hall*.py      # 大厅：图寻 / 数学 / 股市 / 面试 / 道场
│   │   ├── tasks.py · work.py · workflow.py · calendar.py
│   │   └── memory.py · skills.py · mcp.py · terminal.py · git.py · anno.py · aside.py
│   ├── agent/            # 文件 Agent：工具实现 / 沙箱安全闸 / 审批 / 提问 / 撤销 / 子代理
│   ├── reader.py         # 文档版面重建、公式识别、翻译内核
│   ├── reader_export.py  # 译文导出：仅译文 / 双语 PDF、Markdown（reportlab）
│   ├── docparse.py       # PDF / Office 文本提取与扫描件识别
│   ├── llm.py · context.py · prompt_assembly.py · prompts.py
│   ├── search*.py · bilibili.py
│   ├── knowledge.py · notes.py · profile.py · mem_tool.py · memory.py
│   ├── image_gen.py · imaging.py · speech.py · tts.py
│   ├── gal.py · hall.py · interview.py · stock.py · math_arena.py · dojo/
│   ├── task_engine.py · workflow.py · scenes.py · scheduler.py · notify.py
│   ├── plugin_manager.py · mcp.py · skills.py · market.py
│   ├── codeindex.py · debugger.py · trajectory.py · aside.py · anno.py · library.py
│   └── …                 # 其余按功能拆分，详见目录
├── web/                  # 前端界面（无框架、无构建）
│   ├── index.html        # 页面结构 + SVG 图标库（sprite）
│   ├── css/              # base / ide / reader / aside / notes / gal / hall / work …
│   ├── core.js           # 全局状态、HTTP 封装、Toast、SVG helper
│   ├── chat.js · stream.js · composer.js · markdown.js · rich-media.js
│   ├── reader.js · aside.js · agents.js · trajectory.js · turndots.js
│   ├── settings.js · knowledge.js · saves.js · auto.js · init.js · hotkeys.js · hk-core.js
│   ├── work/             # 工作台：core / events / tasks / artifacts / workflows / backup / views / stats / focus / templates / misc
│   ├── git.js · ide-term.js · observe.js · mdlint.js
│   ├── hall.js · hall-geo.js · hall-math*.js · hall-stock.js · hall-interview.js
│   ├── hall-dojo.js · hall-observe.js · hall-gal.js · hall-plugins.js
│   ├── gal/              # GAL 演出（core / assets / stats / script / theme / stage / session / media / voice / editor / ide）
│   └── vendor/           # 本地内置 katex / monaco / pdfjs / seti / vis-network / xterm
├── desktop/              # 可选 Electron 窗口壳（main.js / preload.js / package.json）
├── scripts/              # 开发与运维脚本
│   ├── dev_check.py · http_smoke.py · chat_smoke.py
│   ├── fetch_speech_model.py · fetch_embed_model.py
│   ├── prompt_snapshot.py · sync_presets.py · make_shortcut.ps1 · notify.ps1
│   └── bench.py · eval_research.py · hotkeys_test.js · markdown_test.js · transpage_test.js
├── resources/
│   ├── prompts/          # 提示词模板（*.md，带 {{占位符}}）
│   └── presets/          # 示例角色 / 立绘 / 背景 / BGM / 特效 / 知识库
├── docs/                 # 开发笔记（本地，不入库）
├── data/                 # 运行时数据（整目录忽略，不提交）
├── tests/                # pytest 单测
├── Galgame_monster.bat   # Windows 启动器（%~dp0 自定位）
├── app.ico · requirements.txt · ruff.toml · pytest.ini · .gitignore
└── readme.md             # 本文件
```

---

## 11. 常见问题

**Q：提示「尚未配置聊天 API 密钥」**
到「设置 → 模型接入」填写地址、模型名与密钥并保存。

**Q：公式显示成代码、代码块没有颜色**
KaTeX 已内置在 `web/vendor/katex`；代码高亮依赖 CDN 加载 highlight.js，请确认网络可用。加载失败会降级，不影响聊天。

**Q：关掉控制台窗口后界面还能用吗**
不能。后端服务随该进程运行，请保持控制台窗口开启（最小化即可）。

**Q：点麦克风没反应 / 提示模型未就绪**
先执行 `python scripts/fetch_speech_model.py streaming`，重启服务；首次点麦克风需允许浏览器麦克风权限。降噪还需 `denoise`，精修需 `final`。

**Q：为什么 IDE 的 Tab 补全没有反应**
补全要求先配置 `light_model`。留空时补全不会触发，这是为了避免高频请求打到主模型。

**Q：消息下方的「总输入」为什么比明细里的单轮大几十倍**
「总输入」是整回合累计：每个工具轮都会把完整上下文重发一次。明细表里的逐轮数字是单次调用，计费只按未命中缓存的部分计算。

**Q：Agent 说无法读取 / 写入文件**
先确认已开启 Agent 并选好沙箱目录；AI 只能访问沙箱内路径（越界、`.git` / `.venv` / `data/` 会被拒绝）。若仍不行，重启服务并 `Ctrl+F5` 强刷。

**Q：AI 改了文件，去哪里看它到底改了什么**
看助手回复下方的变更卡：文件名、完整路径、增删行数；点文件名打开 inline diff，可撤销或保留。快捷键 `Ctrl+Backspace` / `Ctrl+Enter`。重启程序后快照失效。

**Q：上传 PDF 提示解析失败 / 读不到内容**
确认已安装 `pypdf` 与 `pypdfium2`；扫描件需要配好支持视觉的模型，且默认只识别前 60 页。也可以在 Agent 沙箱里用 `read_file` 读文本层。

**Q：翻译很慢 / 有段落没译出来**
翻译按章节分批（≤4 段 / 1600 字）并限制并发（默认 3，可改 `reader_translate_parallel`）。失败的段落会走三级重试；仍失败时阅读流会提示，点「翻译本页」或「译全文」可只补缺失段。

**Q：导出的译文 PDF 为什么和原版式不同**
导出是重排文档：统一字号、书签、页码，图表与公式按阅读流顺序插回，不再与原页一一对应。需要原版式时使用左栏原页。

**Q：提问标记在哪里**
悬停任意用户提问 → 点图钉「标记提问」并命名。Solo 模式右侧会出现对应悬浮按钮，点击跳回；右键改名 / 删除。IDE、GAL、大厅、工作台模式下自动隐藏。

**Q：图寻游戏的照片从哪来 / AI 对手好像看不见图**
照片与真实经纬度来自 Panoramax 开放街景库，需要联网。AI 对手要真正看图需配视觉模型；未配置时退用聊天模型，都不可用则区域内盲猜并标注。

**Q：工作台的任务为什么停在「待确认」**
这是有意的安全设计：写文件、建日程等写操作第一次不会自动执行，需在任务详情点「确认执行」。

**Q：工作流到点了没执行**
定时与事件触发都依赖程序正在运行。检查工作流是否启用、时间与星期是否填对（0 = 周一，留空 = 每天）；调度器每 15 秒扫一次，休眠唤醒后会补跑。

---

## 12. 许可证

本项目仅供学习交流使用，请勿用于商业用途。
