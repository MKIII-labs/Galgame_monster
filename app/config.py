# -*- coding: utf-8 -*-
"""接口配置：服务商默认值、配置读写（带缓存）、地址补全与思考参数"""
import copy
import re

from app.store import CONFIG_FILE, read_json, write_json

API_PROVIDERS = {
    "chat": {
        "url_default": "https://api.deepseek.com/chat/completions",
        "model_default": "deepseek-flash",
        "protocol_default": "openai",
    },
    "vision": {
        "url_default": "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
        "model_default": "qwen-vl-plus",
        "protocol_default": "native",
    },
}


DEFAULT_CONFIG = {
    "chat_api_key": "", "chat_url": "", "chat_model": "", "chat_presets": {},
    # 服务商协议：""（按地址自动判断）| openai | anthropic
    # openai 走 /chat/completions 兼容协议；anthropic 走原生 /v1/messages
    "chat_kind": "", "vision_kind": "",
    # 故障转移链：主服务商失败（且一个字都没输出）时，按顺序尝试这些预设名
    "chat_fallback": [],
    # 每日 token 上限：0 = 不限；达到后拒绝发起新请求（按本地用量流水统计）
    "usage_daily_tokens": 0,
    # 分级模型调度：轻量任务（摘要 / 记忆挑选 / GAL 判定 / 语音矫正）可选用的「小模型」。
    # 三项都留空 = 沿用主 chat 模型（默认，行为与从前一致）。
    "light_url": "", "light_api_key": "", "light_model": "",
    # 模型分级策略：off = 纯 API（忽略本地 light，也不启用增量压缩）；
    # on = 轻量任务走 light（可以是本地 Ollama），并按 hybrid_* 预算提前压缩历史；
    # light = 主对话也只走 light（不限定本地还是云端）。
    # 一键关闭即可回到原来的纯 API 行为，不需要改其它配置。
    "hybrid_mode": "off",
    "hybrid_context_limit": 64000,      # 开启时替代 chat_context_limit 的窗口预算
    "hybrid_keep_ratio": 0.45,          # 压缩时保留最近多少比例的原文
    "hybrid_summary_tokens": 8192,      # 摘要输出上限
    "hybrid_quality_gate": 0.55,        # 本地摘要必须保住的原文字符锚点加权召回率
    "hybrid_fallback_api": True,        # 本地摘要不合格 / 失败时回退主模型
    "hybrid_light_chunk_tokens": 3500,  # 本地摘要单次输入上限（8k 上下文留余量）
    "hybrid_local_max_range_tokens": 12000,  # 超过这个区间就改用主模型一次压完（控延迟）
    "hybrid_ide_complete": False,       # 默认不让本地模型接 IDE 内联补全（延迟敏感）
    # 仅轻量模型（hybrid_mode=light）：主对话也走 light。
    # light 是本地服务（Ollama）时上下文只有 8–16k，装不下工具 schema，
    # 此时自动按「无工具闲聊/问答」运行；云端 light 不受这条限制。
    "local_context_limit": 12000,
    "local_reserve_output": 2048,
    "local_keep_ratio": 0.35,
    "local_summary_tokens": 2048,
    "local_max_range_tokens": 10000,
    "chat_think_mode": "", "chat_think_enabled": False, "chat_think_level": "high",
    "chat_native_tools": True,       # 优先用模型原生 function calling；不支持时自动回退文本协议
    # Solo 旁注提问：选中一段文本就地提问，走独立请求，不写会话 / 不调工具。
    "aside_enabled": True,
    "aside_model": "",               # 留空 = 跟随主聊天模型
    "aside_max_tokens": 800,
    # 提问时自动从沙箱代码索引召回相关片段（只读）；没有索引会先后台建一次
    "chat_auto_context": True,
    # Solo 过程折叠区的「智能跟随」：新步骤出现时把它带进视野；用户上翻立即暂停，
    # 回到底部 / 点「回到底部」后恢复。关闭后只更新折叠头统计，不再自动滚动
    #（当前步骤高亮与自动展开 / 收起仍然生效）。
    "ui_process_follow": True,
    # 自动召回的注入预算：高置信的前 N 段内联正文，其余只给 path:行号按需读。
    # 0 = 不设预算（旧行为：最多 5 段全部内联）。
    "auto_context_budget_chars": 8000,
    "auto_context_inline_max": 2,
    # 自动引用条数上限：0 = 不截断，召回多少就列多少（只影响条目数，
    # 正文内联仍受上面两项预算限制）。
    "auto_context_items": 0,
    # 会话第一次提问时注入一眼仓库结构（默认关：会多一次目录遍历；
    # 放在引用里而不是 system，避免文件变化打断前缀缓存）。
    "repo_summary_inject": False,
    # 默认 1M（1,000,000）token：对齐当前主流长上下文模型（Claude / Gemini / Qwen-Long
    # 等 1M 档）的常见窗口，同时保留窗口语义（0 = 完全不限：既不显示占用比例，
    # 也不会触发任何按窗口比例的逻辑）。用户可在「设置 → 上下文与压缩」改成模型真实窗口。
    "chat_context_limit": 1_000_000, "chat_reserve_output": 16384,
    # 上下文压缩：占用达到 ratio 时后台预压缩；保留最近 keep_ratio 的原文；
    # 摘要输出 token 上限。比例按 0–1 存储，设置页用百分数。
    # keep_ratio 的口径对齐 dsh：按 (窗口 - 输出上限) 的比例算，而不是整个窗口。
    "compress_ratio": 0.8,
    "compress_keep_ratio": 0.16,
    "compress_summary_tokens": 8192,
    # 历史「过程旁白」压缩：assistant 的 content 是所有轮次正文的拼接，过程占大头。
    # 最近 keep_turns 条 assistant 消息保留原文，更早的只留「过程摘要 + 最后一段答案」。
    # 0 = 关闭（整段保留）。
    "history_process_keep_turns": 2,
    "history_process_digest": 240,
    # 原文保留区的推进粒度（水位线）：每 step 条 assistant 才整体推进一次边界。
    # 1 = 旧行为（每来一条新 assistant 就有一条旧消息被压成摘要，前缀每轮断一次）；
    # 4 = 边界之间只追加、不改写，缓存可连续命中，代价是最多临时多留 3 条原文。
    "history_rebuild_step": 4,
    # 会话工作记忆（L2）：回合结束后由轻量模型把新增对话合并进一份结构化工作笔记；
    # 压缩时若它已覆盖待压区间且足够新，直接复用，省掉一次压缩调用（零 API 分支）。
    "session_memory_enabled": True,
    # 微压缩（L3）：上下文超过预算 60% 时，对单条过大的工具结果做一次轻量摘要。
    "microcompact_enabled": True,
    "microcompact_min_tokens": 3000,
    "microcompact_trigger_ratio": 0.6,
    # 渐进折叠（L4）：窗口预算用到 80% 时先轻压一档（保留更多原文），
    # 到 100% 再走完整压缩；只影响窗口预算，不影响 40k 工作集。
    "history_collapse_ratio": 0.8,
    "history_collapse_keep_ratio": 0.32,
    # 省费模式（token_profile）：
    #   balanced = 引用正文只在最后一条用户消息内联、历史轮次只留句柄；
    #              界面上下文 / 意图提示 / 自动化目标贴到请求末尾，不破坏 system 前缀。
    #   quality  = 与旧行为逐字节一致（一键回滚，不需要改代码）。
    "token_profile": "balanced",
    # 省费模式的核心：跨回合历史「原文」预算（tokens）。0 = 关闭（回到按窗口压缩）。
    # 只要历史原文超过它，就在后台把更早的部分压进结构化摘要；当前回合永远保留原文。
    # 这不是窗口：模型仍可 read_file / read_tool_result 取回任何细节。
    "history_working_set_tokens": 40000,
    # 超过工作集的多少倍时改为「发送前同步压缩」，避免一上来就发个 20 万 token 的请求。
    "history_working_set_sync_ratio": 1.5,
    # 单回合累计输入预算（tokens）：0 = 不限。默认 3000 万，只拦病态长跑
    # （实测最大单回合 4.65 亿、P99 约 1340 万），正常任务不会触发。
    "agent_turn_token_budget": 30_000_000,
    # 历史附图：把最近几条「带图片的用户消息」重新作为多模态内容附上。
    # 0 = 关闭（历史里只留一行「可回看附件」，模型可能要求用户重新发图）。
    # 每张图约 1600 token；默认 2 条消息 × 最多 2 张。
    "image_history_keep": 2,
    # Anthropic 通道的提示缓存（cache_control）：命中约 0.1x、写入约 1.25x。
    # 只作用于主对话流式请求；轻量任务不缓存（太短，写不划算）。
    "anthropic_cache": True,
    # 发给前端的「当前会话消息窗口」条数（0 = 全部）。
    # 前端已支持分页（msg_total / msg_start + /api/session/messages），
    # 调小可显著缩小 done 事件体积：80 条约 780KB，24 条约 190KB，16 条约 127KB。
    "chat_payload_msg_window": 24,
    # 本会话记忆的自整理：
    # soft_limit：超过这么多条时提示先整理（0 = 不限）；
    # organize_every：每多少个工具轮提醒一次整理（0 = 关）；
    # list_recent：超限时在提示词里列最近多少条，方便合并（12–80）。
    "memory_soft_limit": 60,
    "memory_organize_every": 20,
    "memory_list_recent": 40,
    # 每轮「相关记忆」整块注入的 token 预算（正文仍按单条 400 字截断）。
    "memory_inject_tokens": 3000,
    # 有界记忆索引（对齐 CC 的 MEMORY.md）：常驻标题清单，行数 / 字节双上限（0 = 不限）。
    "memory_index_enabled": True,
    "memory_index_max_lines": 200,
    "memory_index_max_bytes": 25000,
    # 单次模型调用的输出上限（max_tokens，硬截断）；0 = 不传，用服务商默认。
    # 思考模式的 reasoning 也计入这个额度：设得太小，模型会在写工具块之前就被截断
    # （现象是正文停在冒号、却没有工具调用，见 NOTE 43.76）。
    "chat_max_tokens": 8192,
    # 输出上限最多占窗口的多少（对齐 dsh 的 1M 窗口 / 256k 输出比例）；
    # 0 = 不按比例夹紧。用户填得更大也会被夹到这个值，避免「提示词 + 输出」越过窗口。
    "chat_output_window_ratio": 0.25,
    # 除输出上限外，再为估算误差 / 消息元数据留的余量（token）。
    # 可用提示词 = min(窗口*compress_ratio, 窗口 - 输出上限 - headroom)。
    "chat_output_headroom": 8192,
    # 瞬时错误重试策略：normal（有限次，沿用 chat_round_retries / chat_upload_retries）
    # / always（无上限，直到成功或用户停止）。billing / auth / bad_request / context
    # 永不进入无限重试（dsh 的 always 连余额不足也重试，对桌面应用是纯烧钱）。
    "chat_retry_mode": "always",
    # always 模式的安全阀（分钟）：0 = 不限；到点仍未成功就收尾并给「继续任务」。
    "chat_retry_max_minutes": 0,
    # 普通失败（空响应 / 限流 / 5xx）退避：0.5s → 10s，±jitter（对齐 dsh）。
    "chat_retry_backoff_ms": 500,
    "chat_retry_backoff_max_ms": 10000,
    "chat_retry_jitter": 0.1,
    # 传输类失败（上传 / 断网 / 静默）退避：弱网实测 1 秒连撞必失败，保留长退避。
    "chat_transport_backoff_ms": 5000,
    "chat_transport_backoff_max_ms": 30000,
    # 首包看门狗：上游连续多少秒连一个 `data:` 行都没有就中止本轮（秒）；0 = 不限。
    # socket 读超时会被 SSE 注释行续命，只有这一项能兜住「连接活着但正文永远不来」。
    "chat_stream_idle_timeout": 90,
    # 空转看门狗：上游一直在发 `data:` 行，但连续多少秒没有思考 / 正文 / 工具 / 结束标记
    # 就中止本轮（秒）；0 = 关。空 delta / role-only / usage-only 会续命 socket，
    # 却不会让界面出现任何内容 —— 这一项专门兜「有数据但界面全静默」。
    "chat_stream_stall_timeout": 45,
    # 单轮零产出自动重试：这一轮「一个字都没产出」就断了（超时 / 静默 / 连接错误）时，
    # 重发同一份 messages，最多几次；0 = 不重试。有正文或工具调用就绝不重发
    # （否则会拼出两段回答 / 重复执行副作用）。
    "chat_round_retries": 2,
    # 请求体还没发完就断（上行写超时 / 连接被掐）时的独立重试次数。
    # 这种失败没有产出、重发无副作用，而弱网恢复常超过普通预算的 2 次；0 = 跟随普通预算。
    "chat_upload_retries": 4,
    # Code Mode：单次脚本工具调用上限与并发上限；0 = 不限调用次数。
    "code_mode_max_calls": 2000,
    "code_mode_parallel": 16,
    # Code Mode 是否允许脚本直接写文件 / 跑命令（默认关闭）。
    # 打开前脚本被 audit hook 限制为「只读沙箱内文件」：内置 open()、os.remove、
    # subprocess 一律拒绝；打开后写入仍只能落在沙箱内。只在可信任务里开。
    "code_mode_allow_write": False,
    "code_mode_allow_cmd": False,
    # 思考是否落库：开着时刷新会话仍能回看思考；关掉只存正文 / 工具的顺序。
    # 思考只写本地 sessions.json，不会上传。
    "chat_persist_reasoning": True,
    # 每条消息保留的思考字数上限；0 = 不限。超限时丢最早的思考块、保留最近的。
    "chat_reasoning_keep_chars": 12000,
    "chat_balance_url": "",
    "vision_api_key": "", "vision_url": "", "vision_model": "", "vision_protocol": "", "vision_presets": {},
    "vision_use_chat": False, "vision_balance_url": "",
    # 默认：聊天模型支持识图就用聊天模型；勾选此项则优先使用独立的视觉 API
    "vision_prefer_api": False,
    "search_enabled": False, "search_provider": "auto", "search_api_key": "",
    "search_url": "", "search_count": 8,
    # 自动模式的免注册引擎：各自的 Key（留空即用共享免注册额度）与是否参与链
    "search_engine_keys": {}, "search_engine_enabled": {},
    # modsearch 总开关：关闭后不调用免注册引擎链，也不再把网页发给 Firecrawl 云端抓取
    "search_modsearch_enabled": True,
    # 抓正文时先用云端浏览器（Firecrawl，能渲染 JS 页面），失败再回落本地抓取
    "search_fetch_cloud": True,
    # 默认：聊天模型自带搜索（目前是 DeepSeek Search API，复用聊天密钥）优先；
    # 勾选此项则始终使用下面配置的服务商（博查 / 豆包 / Tavily）
    "search_prefer_api": False,
    # B 站数据读取：SESSDATA 是本机登录凭证，只用于请求 B 站，不进入模型上下文
    "bili_sessdata": "", "bili_comment_pages": 3,
    # 注：检索式改写已取消——改由主模型在调用 web_search 时自己给关键词
    "search_fetch": True,            # 抓取命中网页的正文（仅注入提示，不入库）
    "search_fetch_count": 4,         # 抓正文的条数（1–10）
    "search_fetch_reader": "jina",   # 抓正文方式：jina（免 key）| direct
    # 多检索式合并后的条数上限 / 每条正文保留字数。0 = 用代码里的常量（12 / 2500）。
    "search_merged_max": 12,
    "search_fetch_chars": 2500,
    # 复刻 / 还原 / 从零实现类任务：单轮信息量放宽（0 = 用上面两项）。
    "search_research_merged_max": 20,
    "search_research_fetch_chars": 4000,
    # 深度检索（默认开）：所有联网搜索都放宽合并条数 / 正文字数，弱结果自动补搜一次。
    # 关闭后回到 search_merged_max / search_fetch_chars，且不做自动补搜。
    "search_research_enabled": True,
    # 查询级检索缓存：同一 provider + 检索式在 TTL 内直接复用，省额度与延迟。
    "search_cache_enabled": True,
    "search_cache_ttl": 3600,          # 通用查询缓存秒数；时效类会自动缩短
    # 向量混合检索：本地 bge 模型可用时，排序混入 query↔标题/摘要的语义相似度。
    "search_semantic_rank": True,
    # 引用语义校验（P2，可选）：用轻量模型判断 claim 是否真的被引用证据支持；
    # 默认关闭，开启后每次带引用的回答会多一次小模型调用。
    "search_cite_nli": False,
    # 搜索服务商余额接口：留空时按服务商自动识别（bocha / tavily）；volc 无可用接口。
    "search_balance_url": "",
    "learning_presets": {},
    # 已停用的 Skill id 列表；Skill 本体存在 data/skills/<id>/SKILL.md
    "skills_disabled": [],
    # 预设 Skill 是否已播种过（老字段，保留兼容）；新增/删除用下面两个列表精确记录
    "skills_seeded": False,
    "skills_seeded_ids": [],     # 已处理过的预设 id（含用户自己已有的同名目录）
    "skills_deleted": [],        # 用户删除过的预设 id：以后不再播种回来
    "speech_model_dir": "",          # 本地语音模型目录；留空则用 data/models 下的默认目录
    "speech_stream_model_dir": "",   # 流式识别模型目录；留空则用 data/models 下的默认目录
    "speech_polish": False,          # 语音输入结束后是否自动做一次 AI 矫正
    "speech_denoise": False,         # 语音输入是否开启流式降噪（DPDFNet）
    "speech_denoise_model": "",      # 降噪模型路径；留空则用 data/models/dpdfnet_baseline.onnx
    "speech_final_model_dir": "",    # 两遍识别的精修模型目录；留空则用 data/models 下的默认目录
    "ui_mode": "solo",               # 界面模式：solo（纯对话）| ide（工作区）| gal（演出）| hall（功能大厅）| work（工作台）| plugins（插件）
    "session_group_mode": "flat",    # 会话记录：flat = 平铺（默认）；dir = 按沙箱目录分组
    "session_group_pins": [],        # 置顶的分组 key（归一化后的沙箱路径），只影响排序
    "session_group_names": {},       # 分组显示名别名：key -> 名字（不改磁盘目录）
    # ---- 提醒开关（总开关关闭时，下面四项全部不生效）----
    "reminders_enabled": True,          # 总开关
    "reminder_inapp_enabled": True,    # 到点后在会话里留消息 + 应用内提示
    "reminder_system_enabled": True,   # 同时弹 Windows 系统通知（程序关闭也能弹）
    "reminder_calendar_enabled": True, # 日程到点提醒
    "auto_chat_enabled": True,         # 允许 AI 主动聊天（每个会话仍需单独开启）
    "theme": "light",                # 默认浅色（VS Code Light+）
    # ---- IDE 编辑器主题（与上面的界面主题完全解耦，两套配置各自独立）----
    # 空串 = 跟随界面主题；其余取值只在 IDE 模式下生效（见 web/css/ide.css）
    "ide_theme": "vscode-dark",      # vscode-dark | vscode-light | trae-dark | trae-light | trae-blue | ""
    "ide_whitespace": "all",         # 空白字符可视化：none | selection | trailing | boundary | all
    # ---- GAL 演出模式（见 docs/GAL_MODE_DESIGN.md）----
    "gal_emotion_multi": False,      # 情绪字段是否用多标签数组（默认单值）
    "gal_options_instant": True,     # 点选项是否立即发送（否则填回输入框）
    "gal_tts_priority": "sprite",    # 台词发声优先级：sprite=绑定音频优先 | tts
    # ---- GAL 演出皮肤 / 节奏 / 特效 ----
    "gal_theme": "sakura",           # 演出主题：sakura 樱粉 | night 夜蓝 | washi 和风纸 | mono 极简黑白 | cyber 霓虹夜景 | pixel 像素复古
    "gal_typewriter": True,          # 打字机逐字显示
    "gal_cps": 12,                   # 打字机速度（字符/秒）
    "gal_seg_mode": "single",         # 对白展示：single 单行推进（默认，= 新世界观感）| append 逐段追加
    "gal_auto": False,               # 单行推进时：语音播完自动切下一段
    "gal_min_show": 0.6,             # 每段最短展示秒数（自动推进用）
    "gal_fx": True,                  # 关键词特效（*闪光* / *震动* 等）
    "gal_ui_sfx": True,              # 选项界面音效（悬停轻响 / 确认实响，WebAudio 现场合成）
    "gal_nudge": True,               # 本轮没给选项时，在段末提示玩家可主动发言（文案由 AI 按场景生成）
    "gal_show_think": False,         # 是否显示 AI 思考过程（关闭时只显示「思考中…」，更不破坏沉浸感）
    "gal_think": False,              # GAL 是否**启用**深度思考（默认关：实测首 token 差约 10 倍）
    "gal_show_exec": True,           # 是否显示 AI 行为（搜索/工具等，以「幕间」呈现）
    # 下面三项由演出界面直接消费：显示状态参数 / GAL 允许联网 / 自动隐藏鼠标指针
    "gal_show_stats": True,
    "gal_search": False,
    "gal_hide_cursor": True,
    "gal_dialog_max": 46,            # 对白框最大高度（占舞台高度百分比，30–70）
    "gal_sprite_fx": True,           # 立绘进出场 / 换人过渡动画
    "gal_bar_auto": True,            # 顶部工具条沉浸式淡出（鼠标移入才实显）
    "gal_bgm_volume": 0.5,           # 舞台 BGM 音量（0–1）
    # ---- 界面：可收起的区域（输入框 / 侧栏 / IDE 文件管理区）----
    "composer_collapsed": False,      # 输入框是否收起
    "sidebar_collapsed": False,       # 左侧会话栏是否收起
    "ide_side_collapsed": False,      # IDE 文件管理区（活动栏右侧视图）是否收起
    # ---- 功能大厅（AI 互动玩法，见 app/hall.py 与 web/hall.js）----
    # 大厅模块内部的 AI 开关（id -> bool）；默认开启，可在模块里关掉独自游玩
    "hall_ai_enabled": {},
    "hall_stock_ai_count": 3,         # 模拟股市：AI 对手人数（1–3）
    # ---- GAL AI 自动写剧本 ----
    "gal_auto_script": False,        # 总开关：由 AI 依据角色设定与用户画像自己编剧情并推进
    "gal_auto_script_acts": 5,       # 大纲幕数（1–12）
    "gal_auto_script_rounds": 4,     # 每幕最多回合数（1–12）
    # ---- GAL 台词朗读（TTS）----
    "tts_enabled": False,            # 是否朗读台词
    "tts_provider": "edge",           # edge（免费·免密钥）| openai（兼容 /v1/audio/speech）| gpt-sovits（本地 HTTP）
    "tts_url": "",                   # edge 忽略；openai 留空用 api.openai.com；gpt-sovits 留空用 127.0.0.1:9880
    "tts_api_key": "",
    "tts_model": "",                 # openai 用，如 tts-1 / gpt-4o-mini-tts
    "tts_voice": "",                 # edge: zh-CN-XiaoxiaoNeural 等；openai: alloy 等；留空用各自默认
    "tts_speed": 1.0,
    "tts_max_len": 80,               # 单句最大字数（按标点切句）
    # ---- IDE 内联补全（Tab 补全）----
    # 默认开，但没有配置 light_model 时不会触发（避免用主模型每 350ms 烧一次 token）。
    "ide_ai_complete": True,
    "ide_ai_complete_delay_ms": 350,  # 光标停顿多久后请求
    "ide_ai_complete_max_lines": 8,   # 一次最多补几行
    # ---- Agent 执行上限（设置 → Agent；缺省值等于 agent.py 里的常量，不配置时行为不变）----
    # 下面五项都能填 0 = 不限：上限由你自己取舍，代码只守最小值（负数视为填错，回落默认）。
    "agent_max_rounds": 0,           # 单轮对话最多循环几次「调工具 → 再问模型」；0 = 不限
    "agent_auto_continue": 3,        # 任务没做完时自动续跑几次；0 = 关闭，-1 = 无上限（1–10）
    "agent_repeat_limit": 5,         # 同工具同参数连续几次就拦截；0 = 不拦截
    "agent_cmd_timeout": 60,         # 命令默认超时（秒）；0 = 不限，直到命令自己结束或你点停止
    # 只读工具并发预取：一轮里的多个读取一起跑（写之后的同文件读、命令之后的读除外）。
    # 0 = 关闭（全部串行）；上限防止模型一次发几十个读把机器打满。
    "agent_prefetch_max": 24,
    "agent_prefetch_workers": 10,
    # 执行阶段只读并行：连续的只读调用按段并发执行（写 / 命令 / 交互是屏障）。
    # 0 = 关闭；与预取不同，屏障之后的只读段也能并行（例如「跑测试 → 读 B/C/D」）。
    "agent_tool_parallel": True,
    "agent_tool_parallel_max": 10,
    # 写前读：覆盖已有文件前必须先在会话里读过；读后文件被外部改动（mtime / size 变化）
    # 会要求重读，避免模型拿过期内容整体覆盖。新建文件不受影响。
    "agent_require_read": True,
    # 同回合重复读去重：同一回合里再读同一个「版本没变」的文件时，只回一行
    # （文件名 / 大小 / 已读过），不再把全文重发一遍。带 start / limit / raw 时照常读。
    "agent_read_dedupe": True,
    # 大文件默认给符号大纲：只对 .py / .js / .ts 且行数达到阈值、且没带 start / limit 时生效。
    # 默认关——它会改变 read_file 的返回形态，需要先用 eval 确认模型适应。
    "read_outline_mode": False,
    "read_outline_min_lines": 600,
    # 验收：不再「开工前强制定义机检项」——标准会随产物漂移，而且容易为了过闸凑一条。
    # 现在只在多步任务没定义时软提醒一次；收尾由验收子代理按实际产物核对。
    # ---- 验收子代理（可信保真闸门）----
    # finish 之前另起一个独立 AI：与主模型同等工具权限，自己做核对后打分。
    # 覆盖所有任务（不再只限视觉）；没有证据的「通过」不算通过，客观事实会给分数封顶。
    # 每回合最多复核几次；连续失败到第 3 次时允许模型用 quality_waiver 收尾。
    "agent_acceptance_review": True,
    "agent_acceptance_min": 70,
    "agent_acceptance_max_reviews": 2,
    "agent_acceptance_rounds": 12,     # 验收代理自己的工具轮次；0 = 不限
    "agent_acceptance_timeout": 0,     # 单次验收超时秒数；0 = 用内置 180 秒
    "agent_acceptance_model": "",      # 验收用的模型；留空 = 跟随子代理 / 主聊天模型
    # ---- 计划审批（Claude 式 plan mode）----
    # 第一份计划出现后先挂起，等用户批准 / 提意见 / 逐条改步骤，然后再执行。
    # /plan 规划模式始终生效；普通任务可用本开关关闭。0 = 用内置 1800 秒超时。
    "agent_plan_review": True,
    "agent_plan_review_timeout": 0,
    # 同一目标连续失败几次后追加「先诊断、别只调参数」的熔断提示；0 = 关闭。
    "agent_fail_streak": 2,
    # 单轮任务时间预算（分钟）；0 = 不限。到 80% 提醒先交最小切片，到 100% 停止本轮。
    "agent_task_budget_min": 0,
    # 每轮开始检查本会话写过的文件是否在工具调用之外消失；发现后提示 recover_file。
    "agent_file_watch": True,
    # 沙箱里已有 Git 仓库且工作树不干净时，每个会话提示一次。
    "agent_sandbox_warn": True,
    # 是否把「功能大厅（模拟股市 / B 站）」的说明与界面数据注入系统提示。
    # 日历 / 日程始终注入，不受这个开关影响。
    "agent_hall_prompt": True,
    # 工具结果超过这个字符数就落盘：只回头部 + 关键行 + 尾部 + locator，
    # 模型可用 read_tool_result 跨轮取回。0 = 关闭落盘（退回「头尾截断 + 请求内句柄」）。
    # 12000 ≈ 3~4k token：再往上就会在后续每一轮里重发，收益远低于一次按需读取。
    "tool_spill_max": 12000,
    "tool_spill_keep": 400,
    "tool_spill_mb": 256,
    # 单条工具结果回灌预算：超 max 时回灌「头 head + 尾 tail」，中间省略。
    # 0 = 不截断；超过这个字符数才头尾裁剪（默认 30000，对齐 Claude Code 的内联阈值）。
    # 旧结果折叠保留最近 keep_recent 条原文，其余压成 fold_chars 以内的结构化摘要。
    "tool_result_max": 30000,
    "tool_result_head": 4096,
    "tool_result_tail": 1024,
    "tool_result_keep_recent": 2,
    "tool_result_fold_chars": 240,
    # 工具结果总量触发（真实 token）：最近一次响应返回的 prompt_tokens + 之后新增内容的估算
    # 超过这个值，就按「单条结果」把较早的折叠成摘要。0 = 不按 token 触发。
    "tool_result_trim_tokens": 100000,
    # 低水位（tokens）：触发后一次折到这里，而不是折到刚好卡线。
    # 前缀缓存按「改一个字节、后面全废」计费，折叠**次数**比折叠深度更贵；
    # 折到刚好卡线会导致下一轮又超、再折一条，历史中段每轮都被改写。
    # 0 = 自动取高水位的 55%。
    "tool_result_trim_target": 0,
    # 子代理的独立上下文预算（tokens）：每个侦察兵只看自己的 messages，超预算就折自己的旧结果，
    # 与主对话互不影响。0 = 不限。
    "subagent_context_tokens": 60000,
    # 子代理单次模型调用的输出上限（tokens）。0 = 不限，用服务商默认。
    "subagent_max_tokens": 0,
    # 子代理最多来回几轮（调工具 → 再问模型）。0 = 不限。
    "subagent_max_rounds": 30,
    # 外联审计：记录命令（含子进程）连过哪些外部地址，只记录、不拦截。
    # 关掉后命令结果里不再附「外部连接」摘要。
    "agent_net_audit": True,
    # ---- 并行探索（子代理，只读）----
    # 主代理可派 1–N 个只读侦察兵并行查代码；每个只回 600 字结论，原文不进主上下文。
    "agent_explore_enabled": True,
    "agent_explore_max": 8,          # 一次最多几个子代理（默认 8，硬上限 12）
    "agent_explore_budget": 160000,  # 整批子代理的 token 预算；0 = 不限
    "agent_explore_model": "",       # 子代理用的模型；留空 = 跟随主聊天模型
    "agent_explore_ttl": 600,        # 子会话保留秒数（explore_followup 追问）；0 = 关闭
    # 真 fork：把父会话已完成的历史（受预算裁剪）带给子代理；关掉退回 2000 字摘要。
    # fork_tokens 是前缀预算（0 = 不限），并会与 agent_explore_budget 联动取较小值。
    "agent_explore_fork": True,
    "agent_explore_fork_tokens": 12000,
    # 子代理写入：需要模型显式 allow_write，且每次写仍走主循环审批。0 = 不限次数。
    "agent_explore_write": True,
    "agent_explore_write_max": 16,
    # 结论里的「文件:行号」重新读取核对（引用不存在则重试一次）
    "agent_explore_verify": True,
    # 嵌套：根 depth=0，子代理最多再往下 max_depth 层；max_total 是一棵子代理树的总数上限。
    # 嵌套层默认只读；nested_write 打开后仍走主循环审批。
    "agent_explore_max_depth": 5,
    "agent_explore_max_total": 100,
    "agent_explore_nested_write": False,
    # 后台批次：启动后立即返回；status / result / cancel 取用。并发与结果保留时间可调。
    "agent_explore_background": True,
    "agent_explore_background_max": 12,
    "agent_explore_background_ttl": 86400,
    # ---- 自动派发（复用上面的子代理团队，只派只读代理）----
    # route：系统提示里给出团队清单与派活时机；连续多次只读还没派活时提醒一次。
    # plan：回合开始先跑 planner，把计划注入主上下文（不动手、不写文件）。
    # review：本轮有写入时，finish 之前派 reviewer 复核（更慢、更贵，默认关）。
    "agent_auto_route": True,
    "agent_auto_plan": True,
    "agent_auto_review": False,
    # ---- 脚本编排 / 并行写隔离 ----
    # 脚本（run_workflow_script）里用 wf.agent / wf.agents / wf.pipeline 调用命名子代理；
    # 写权限跟随代理定义，每次写仍走审批。agent_script_agent_calls = 一段脚本最多调几次（0 = 不限）。
    "agent_script_agents": True,
    "agent_script_agent_calls": 8,
    # off = 不登记；serial = 同批重复写同一文件时回灌提醒（默认）；strict = 拒绝第二次写。
    "agent_write_isolation": "serial",
    # 本地 worktree 隔离（只对 git 沙箱）：off = 关闭；auto = 本批 ≥2 个可写子代理时
    # 每个代理各拿一份 git worktree，跑完产补丁、审批后回写主沙箱；always = 一律隔离。
    # 非 git 沙箱自动退回上面的「串行写 + 冲突提示」，不做半套隔离。
    "agent_worktree_isolation": "auto",
    # ---- 通用还原质量（参考物 vs 产物）----
    # 只在「本轮用过 image_compare 且最佳总分 < agent_fidelity_min」时生效，
    # 与任务领域无关：off = 不拦；nudge = 第一次 finish 提醒一次；strict = 未达标一直拦。
    # 模型可以在 finish 里传 quality_waiver 说明为什么不再迭代。
    "agent_fidelity_gate": "nudge",
    "agent_fidelity_min": 70,
    # 原生视觉附图：see_image 默认把沙箱图片直接附到本轮上下文（主模型自己看像素），
    # 而不是只返回一段文字描述。需要聊天模型支持图片；失败时请关掉此开关。
    "agent_native_vision": True,
    # ---- 轨迹（模型视角的追加式事件账本）----
    # 记录系统提示、上下文注入、思考、工具调用/结果、子代理、计划与压缩；
    # 只读旁路，不进入模型上下文。max_field 是单字段保留字符数（0 = 不限）。
    "trajectory_enabled": True,
    "trajectory_max_field": 200000,
    # ---- 全仓代码库语义索引 ----
    # 本地 embedding + BM25；默认不限文件 / 块数（0 = 不限），超大仓库可在这里收紧。
    "codeindex_enabled": True,
    "codeindex_max_files": 0,
    "codeindex_max_chunks": 0,
    "codeindex_max_file_kb": 512,
    # ---- 研究闸门（复刻 / 还原 / 从零实现类任务）----
    # 这类任务的信息缺口在外部（规格、机制、参数），光读本地代码会「凭记忆硬写」。
    # 打开后：写第一个文件之前，至少要先检索；缺口用 research_note 记账，跨轮保留。
    "agent_research_gate": True,
    "agent_research_min_searches": 2,   # 动笔前最少检索次数；0 = 不设下限
    "agent_research_fetch_count": 6,    # 研究类搜索抓正文的条数（0 = 用 search_fetch_count）
    # 持久化工作流（编排层）：每步落盘、可 resume；写 / 命令仍走审批。
    # max_steps / max_parallel / keep 填 0 = 不限（keep=0 表示不清理历史文件）。
    "workflow_enabled": True,
    "workflow_max_steps": 48,
    "workflow_max_parallel": 10,
    "workflow_keep": 200,
    # 「查看变更 / 撤销」靠撤销快照：每个会话保留多少条**往期**写入（本轮写入的不受此限）。
    # 0 = 不限（一直留到进程退出）；调大更占内存，但能回看更早的改动。
    "agent_max_undo": 60,
    # Shell 自由度：strict（默认，禁管道/重定向，与从前一致）
    #              standard（允许 | 与沙箱内重定向，每段仍过白名单）
    #              trusted（额外允许 ; && || 串联，仅建议手动会话使用）
    "agent_shell_mode": "standard",
    # 写后自动格式化：off（默认）| auto（有 ruff / prettier 就用）
    # 只调用本机已安装的工具，不用 npx 临时下载；失败一律保留原文件。
    "agent_formatter": "off",
    "autopilot_max_turns": 30,       # 自动化：单个目标最多跑几个回合（回合 = 一次完整对话请求）
    "autopilot_max_minutes": 90,     # 自动化：单个目标最长跑多久（分钟）
    # 自动化重构后的策略（2026-10-27）：
    # use_budget = False 时上面两项只作展示，不硬刹；改用「连续无进展」看门狗。
    "autopilot_use_budget": False,
    "autopilot_watchdog_turns": 4,   # 连续几回合没有新写入 / 计划变更 / 验收变化就干预；0 = 关闭
    "autopilot_stall_minutes": 60,   # 多久没有任何进展就暂停（含一直重试）；0 = 关闭
    "autopilot_retry_max": 0,        # 瞬态错误最多重试几次；0 = 不设上限（仍有 stall 兜底）
    "autopilot_resume_on_start": True,   # 应用重启后自动接回未完成目标
    "autopilot_verify": True,        # 没有机检命令时，用独立评审员判定 rubric
    # 遇到提问卡 / 正文决策问句时的策略：auto（一律自决）/ smart（可逆自决、不可逆问人）/ ask（一律问）
    "autonomy_question_policy": "smart",
    # 上面这档策略的生效范围：autopilot（仅自动化，默认）/ all（所有对话）
    "question_policy_scope": "autopilot",
    # 任务 / 提问的通知：横幅、声音、按事件开关、音量（0-100）
    "notify_banner": True,
    "notify_sound": True,
    "notify_done": True,
    "notify_ask": True,
    "notify_error": True,
    "notify_volume": 60,
    # 插件：加载 data/plugins/*.py（每个文件提供 register()）。默认关闭 —— 插件是任意 Python 代码。
    "plugins_enabled": False,
    # 单个插件的停用名单（目录包 / 单文件插件的 id）；启停后需重启进程
    "plugins_disabled": [],
    # ---- MCP（外部工具服务）----
    # 默认关闭：stdio server 是任意代码、权限与主进程相同；远程 server 意味着数据出境。
    # 每个 server 形如：
    #   {"name":"fs","transport":"stdio","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem","D:/work"],
    #    "env":{},"cwd":"","enabled":true,"readonly_tools":["read_*"],"timeout":60}
    #   {"name":"api","transport":"http","url":"https://example.com/mcp","headers":{}}
    "mcp_enabled": False,
    "mcp_servers": [],
    # lazy = 只挂 mcp_list_servers / mcp_list_tools，模型要用哪个 server 才挂它的工具（省上下文）
    # always = 所有已启用 server 的工具常驻
    "mcp_tool_mode": "lazy",
    "mcp_timeout": 60,
    # ---- Hooks（生命周期钩子）----
    # 默认关闭：command 钩子会执行任意命令，python 钩子会加载插件代码。
    # 结构：{"PreToolUse":[{"type":"command","command":"python hooks/audit.py","timeout":10}],
    #        "PostToolUse":[{"type":"python","handler":"my_hook"}]}
    "hooks_enabled": False,
    "hooks": {},
    # ---- pyright 语言服务（类型检查 / 跳转定义 / 引用 / 重命名）----
    # 首次调用会用 npx 拉取 pyright（走 npmmirror，约 50MB）；关掉后模型退回正则版 find_symbol。
    "lsp_enabled": True,
    # ---- Git ----
    # AI 提交策略：off = 不给模型 git_commit；ask = 每次确认；auto = 直接提交
    "git_commit_mode": "ask",
    # 提交前验证：auto（默认，用符号图找相关测试并只跑它们）| off（不验证）
    # 只跑「受改动影响的测试」，不跑全量；失败会拒绝提交并把失败摘要回灌给模型。
    "git_verify_before_commit": "auto",
    # ---- 响应速度相关（默认值都取「更快」，需要精度时再手动打开）----
    # 用轻量模型从候选里挑记忆：更准，但发生在首字节之前（实测 p90 1.9s / 最坏 7s）
    "memory_llm_pick": False,
    # 会话内容的真相源：events = 启动时以事件流为准（sessions.json 只作快照与列表）；
    # json = 旧行为（只读 sessions.json）。事件流始终会写，切换只影响读取。
    "session_store": "events",
    # 「是否读取用户画像」已改为会话级开关（session.use_profile，默认开），
    # 在「知识库 → 本会话使用的知识库」里按会话管理，不再有全局配置项。
    # 「AI 自动记录画像」是另一件事，由 profile.auto_learn_enabled 单独控制。
    # 能力工具下发方式：auto = 按本轮意图只挂相关域（bili / calendar / stock / memory / observe）
    #                  always = 全部常驻（旧行为，schema 更大、首 token 更慢）
    "agent_cap_tools": "auto",
    # ---- Computer Use（受控浏览器）----
    # 默认关闭：开启后模型多出 5 个浏览器工具，并且会真的弹出一个 Edge 窗口。
    "computer_enabled": False,
    # 注意：computer_site_mode 不写默认值。load_config 会自动补默认键，一旦补成 "ask"
    # 就再也走不到「旧键 computer_browser_unrestricted=true → allow」的兼容分支。
    # 新装用户没有这个键 → site_mode() 回退 ask；用户保存设置后才会显式写入。
    # 旧键：兼容旧配置与旧客户端；true 等价于 computer_site_mode=allow
    "computer_browser_unrestricted": False,
    # 允许操作的站点（host 或 *.host）。留空 = 每个新站点都先问你。
    "computer_sites": [],
    # 额外禁止的站点（内置的银行 / 支付 / 邮箱 / 密码管理器名单不可解除）
    "computer_extra_block": [],
    # 单次快照最多回灌多少字（太大费 token，太小看不清页面）
    "computer_snapshot_chars": 4000,
    # ---- Computer Use · 桌面（Windows UIA）----
    # 与浏览器开关独立：桌面能碰到一切，默认关，且必须先填白名单才有窗口可用。
    "computer_desktop_enabled": False,
    # 无限制：不再检查桌面程序白名单（提权窗口、密码框等硬限制仍生效）
    "computer_desktop_unrestricted": False,
    # 允许操作的桌面程序（exe 名，如 notepad.exe；支持 *.exe）
    "computer_apps": [],
    # 浏览器后端：local = 独立 profile 的受控窗口；extension = 扩展桥（复用登录态）
    "computer_backend": "local",
    # 结构化信息不足（页面全是 canvas / 控件为 0）时自动补一张截图给视觉模型
    "computer_auto_shot": True,
    # 扩展桥配对密钥：首次生成后持久保存（扩展设置里粘贴一次即可）
    "computer_bridge_key": "",
    # ---- 图片生成（本地 ComfyUI）----
    # 默认关闭：需要单独安装 ComfyUI、下载 FLUX 3 Image Klein 权重与工作流；
    # 不开启时接口直接报错，不占资源，也不影响其他功能。
    "image_enabled": False,
    "image_comfy_url": "http://127.0.0.1:8188",
    # ComfyUI「另存为 API 格式」导出的工作流；留空 = data/image_workflow.json
    "image_workflow": "",
    # 单张图的整体等待上限（秒）：首次加载模型会慢，别设太小
    "image_timeout": 180,
    # 输出目录；留空 = data/generated
    "image_dir": "",
}


def default_config():
    """返回一份全新的默认配置（深拷贝，调用方可随意修改）"""
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config():
    data = read_json(CONFIG_FILE)
    if not isinstance(data, dict):
        data = {}
    # 兼容旧版 deepseek_* / dashscope_* 键名
    for old, new in (("deepseek", "chat"), ("dashscope", "vision")):
        for suf in ("api_key", "url", "model", "protocol", "presets"):
            ok = f"{old}_{suf}"
            nk = f"{new}_{suf}"
            if ok in data and not data.get(nk):
                data[nk] = data[ok]
    for key, val in DEFAULT_CONFIG.items():
        if key not in data:
            data[key] = copy.deepcopy(val)
    return data


# 已废弃的配置键：保存时顺手清掉，避免前端回填旧 config 时又写回来。
_OBSOLETE_KEYS = {
    "search_rewrite",            # 检索式改写已取消（主模型自己给关键词）
    "profile_inject_enabled",    # 画像读取已改为会话级 session.use_profile
    "hall_ai_count",             # 大厅改用 hall_ai_enabled（按人开关）
    "gal_auto_script_replan",    # 从未被读取
    "chat_protocol",             # 聊天侧协议由地址自动判断，不再单独配置
}


_LIST_KEYS = ("computer_sites", "computer_apps", "computer_extra_block")


def _list_value(value):
    """列表型配置统一成数组：兼容旧客户端传来的换行 / 逗号分隔字符串。"""
    if isinstance(value, str):
        parts = re.split(r"[\s,;，；]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.extend(re.split(r"[\s,;，；]+", item))
            elif item:
                parts.append(str(item))
    else:
        return []
    return [p for p in (str(x).strip() for x in parts) if p]


def save_config(config):
    if isinstance(config, dict) and any(k in config for k in _OBSOLETE_KEYS):
        config = {k: v for k, v in config.items() if k not in _OBSOLETE_KEYS}
    if isinstance(config, dict):
        config = dict(config)
        for key in _LIST_KEYS:
            if key in config:
                config[key] = _list_value(config[key])
    write_json(CONFIG_FILE, config)


def resolve_endpoint(url):
    url = (url or "").strip()
    if not url:
        return url
    base = url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"

def normalize_content(content):
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return content if content is not None else ""

def get_api_protocol(provider):
    p = API_PROVIDERS[provider]
    return (load_config().get(f"{provider}_protocol", "") or "").strip() or p["protocol_default"]


def get_api_kind(provider="chat", cfg=None):
    """协议族：openai（OpenAI 兼容）/ anthropic（原生 Messages）。

    显式配置优先；否则按接口地址判断——这样用户只换 URL 也能自动走对协议。
    仅轻量模型模式下 chat 实际用的是 light 的地址，协议也按它判断。
    """
    from app import providers
    cfg = cfg or load_config()
    if provider == "chat" and light_chat_mode(cfg):
        url = (cfg.get("light_url") or "").strip()
        return providers.kind_of(url, cfg.get("light_kind") or "")
    url = cfg.get(f"{provider}_url", "").strip() \
        or API_PROVIDERS.get(provider, {}).get("url_default", "")
    return providers.kind_of(url, cfg.get(f"{provider}_kind") or "")


def is_local_url(url):
    """本地推理服务（Ollama / LM Studio 之类）：用地址里的端口 / 关键字判断。"""
    u = str(url or "").lower()
    return bool(u) and ("11434" in u or "ollama" in u)


def hybrid_on(cfg=None):
    """混合 / 仅轻量模型：都会启用轻量模型与更积极的压缩。"""
    cfg = cfg or load_config()
    return str(cfg.get("hybrid_mode") or "off").strip().lower() in ("on", "light")


def light_chat_mode(cfg=None):
    """仅轻量模型：主对话也走 light（不限本地 / 云端）。"""
    cfg = cfg or load_config()
    return str(cfg.get("hybrid_mode") or "off").strip().lower() == "light"


def light_chat_is_local(cfg=None):
    """仅轻量模型 + light 指向本地服务：上下文只有 8–16k，需要关掉工具。"""
    cfg = cfg or load_config()
    return light_chat_mode(cfg) and is_local_url(cfg.get("light_url"))


def get_api_conf(provider, cfg=None):
    cfg = cfg or load_config()
    # 仅轻量模型：主对话复用 light；缺地址 / 模型时返回空，
    # 让上层给出「请先配置」的明确错误，而不是悄悄回落到主 API。
    if provider == "chat" and light_chat_mode(cfg):
        lu = (cfg.get("light_url") or "").strip()
        lm = (cfg.get("light_model") or "").strip()
        if not (lu and lm):
            return "", "", ""
        lk = (cfg.get("light_api_key") or "").strip()
        if not lk and is_local_url(lu):
            lk = "ollama"      # 本地服务不校验 key，但请求头需要占位
        if get_api_kind("light", cfg) == "openai":
            lu = resolve_endpoint(lu)
        return lk, lu, lm
    p = API_PROVIDERS[provider]
    key = cfg.get(f"{provider}_api_key", "").strip()
    url = cfg.get(f"{provider}_url", "").strip() or p["url_default"]
    model = cfg.get(f"{provider}_model", "").strip() or p["model_default"]
    # Anthropic 的地址不能补成 /v1/chat/completions，保持原样由适配层补 /messages
    if get_api_kind(provider, cfg) == "openai":
        url = resolve_endpoint(url)
    return key, url, model

MAX_RESERVE = 32000


def model_context_limit(model="", cfg=None):
    """上下文窗口（tokens）：只认用户填的 `chat_context_limit`。

    0 = 不限：不做前缀猜测、不查任何内置表。调用方据此关掉所有「按窗口比例
    压缩」的逻辑，只保留请求体字符上限兜底。
    model 参数保留是为了兼容既有调用方，不再参与计算。
    """
    cfg = cfg or load_config()
    try:
        n = int(cfg.get("chat_context_limit") or 0)
    except (TypeError, ValueError):
        n = 0
    return max(0, n)


def provider_chain(cfg=None):
    """可尝试的服务商链：主配置 + chat_fallback 里的预设。

    返回 [(name, kind, key, url, model), ...]；只包含配置完整的项。
    """
    from app import providers
    cfg = cfg or load_config()
    if light_chat_mode(cfg):
        # 只挂 light 一条：失败就是失败，避免悄悄回落到主 API
        key, url, model = get_api_conf("chat", cfg)
        return [("main", get_api_kind("chat", cfg), key, url, model)]
    out = []
    key, url, model = get_api_conf("chat", cfg)
    out.append(("main", get_api_kind("chat", cfg), key, url, model))
    presets = cfg.get("chat_presets") or {}
    for name in cfg.get("chat_fallback") or []:
        p = presets.get(str(name)) if isinstance(presets, dict) else None
        if not isinstance(p, dict):
            continue
        url = str(p.get("url") or "").strip()
        key = str(p.get("key") or p.get("api_key") or "").strip()
        model = str(p.get("model") or "").strip()
        if not (url and model):
            continue
        kind = providers.kind_of(url, p.get("kind") or "")
        if kind == "openai":
            url = resolve_endpoint(url)
        out.append((str(name), kind, key, url, model))
    return out


def effective_think_mode(url):
    """思考参数模式：off / openai / deepseek / ollama；配置为空时按接口地址自动判断"""
    mode = (load_config().get("chat_think_mode") or "").strip()
    if mode in ("off", "openai", "deepseek"):
        return mode
    if is_local_url(url):
        # Ollama 的 OpenAI 兼容端点默认思考；关闭时必须显式发 reasoning_effort=none
        return "ollama"
    return "deepseek" if "deepseek" in (url or "").lower() else "openai"

def thinking_payload(mode, enabled, level):
    """生成思考相关请求参数。

    - off      : 不发送任何思考参数
    - openai   : 开启时发 reasoning_effort，关闭时不发（无法表达"禁用"）
    - deepseek : 始终发 thinking.type；开启时附带 reasoning_effort
    - ollama   : 开启时发 reasoning_effort，关闭时发 reasoning_effort=none
    """
    level = (level or "high").strip() or "high"
    if mode == "off":
        return {}
    if mode == "ollama":
        return {"reasoning_effort": level if enabled else "none"}
    if mode == "openai":
        return {"reasoning_effort": level} if enabled else {}
    out = {"thinking": {"type": "enabled" if enabled else "disabled"}}
    if enabled:
        out["reasoning_effort"] = level
    return out


def use_chat_for_vision():
    """识图走哪条通道。

    默认用聊天模型识图（图片直发多模态，少一次单独调用）；只有用户勾选
    「优先使用视觉 API」时才改走视觉 API。本地轻量模型是纯文本模型，
    这时不再把图片发给它，改走独立的视觉 API。
    """
    cfg = load_config()
    if light_chat_is_local(cfg):
        return False
    if bool(cfg.get("vision_prefer_api")):
        return False
    return bool(get_api_conf("chat", cfg)[0])
