# -*- coding: utf-8 -*-

from app import tool_meta

from .dispatch import _HANDLERS
from .groups import (PLAN_MODE_TOOLS, SEE_MAX_IMAGES, _PLUGIN_TOOLS, _PLUGIN_TOOLS_LOCK, _json_type, hidden_tools)


def _fn_tool(name, description, props=None, required=None):
    # additionalProperties=False：schema 层就告诉模型「别塞没声明的字段」，
    # 参数拼错时更容易在生成阶段被自己发现，而不是执行阶段才报错。
    params = {"type": "object", "properties": props or {},
              "additionalProperties": False}
    if required:
        params["required"] = required
    return {"type": "function",
            "function": {"name": name, "description": description,
                         "parameters": params}}


# ---------------------------------------------------------------- 工具分层
# 每轮请求都会重发整份 tools schema，低频工具的说明长期占着上下文预算。
# 这里把「只在特定任务里用得上」的工具归组：默认不下发 schema，
# 命中意图关键词时整组挂载，或由模型调用 tool_search 解锁（下一轮生效）。


def _tool_search_schema():
    return _fn_tool(
        "tool_search",
        "查找并解锁本轮未下发的工具组（visual / image / asset / audio / workflow / "
        "utility / diagram）。解锁后下一轮即可直接调用",
        {"query": {"type": "string",
                   "description": "组名或关键词，如 visual / 素材 / 音频 / 提醒"},
         "group": {"type": "string", "description": "可选：直接指定组名"}},
        ["query"])


def native_tool_schemas(agent_on=False, search_ready=False, cap_domains=None,
                        computer_on=False, lsp_on=False, git_commit_mode="off",
                        plan_mode=False, allow_groups=None, unlocked=None,
                        allowed_tools=None):
    """生成 OpenAI 兼容的 tools 列表。

    原生 function name 不允许点号，所以能力名里的 `.` 换成 `_`
    （calendar.create → calendar_create）；收到调用后 normalize_tool_name
    会再映射回带点号的规范名。

    cap_domains：只挂这些前缀的能力（如 {"bili", "calendar"}）；None = 全挂。
    B 站 / 股市 / 日程这类低频能力不该每轮都占 schema 预算，由调用方按本轮意图筛。

    lsp_on：是否挂 LSP 工具（关掉后模型只能用正则版 find_symbol）。
    git_commit_mode：off = 完全不给 git_commit；ask / auto 只是审批策略不同。
    allowed_tools：按旧工具名授权的子集（子代理 / 巡检）；meta 只暴露被授权的 action。
    """
    out = []
    if agent_on:
        out += [
            _fn_tool("list_dir", "列出目录内容", {"path": {"type": "string"}}, ["path"]),
            _fn_tool("read_file", "读取文件（带行号；单次最多 1MB，超过用 start / limit 分段）",
                     {"path": {"type": "string"},
                      "start": {"type": "integer", "description": "起始行，从 1 开始"},
                      "limit": {"type": "integer", "description": "最多读多少行"},
                      "raw": {"type": "boolean",
                              "description": "true = 返回原文（不加行号），便于精确匹配与统计"}},
                     ["path"]),
            _fn_tool("read_files",
                     "一次读取多个文件（最多 16 个）。要看多个文件时优先用它，少跑几轮",
                     {"paths": {"type": "array", "items": {"type": "string"},
                                "description": "文件路径列表（沙箱内相对路径）"},
                      "start": {"type": "integer", "description": "起始行，从 1 开始"},
                      "limit": {"type": "integer", "description": "每个文件最多读多少行"},
                      "raw": {"type": "boolean",
                              "description": "true = 返回原文（不加行号）"}},
                     ["paths"]),
            _fn_tool("search_files",
                     "按正则搜索文件内容（每个文件默认列 5 处，可带上下文或只统计数量）",
                     {"pattern": {"type": "string"},
                      "glob": {"type": "string", "description": "文件名过滤，如 *.py"},
                      "context": {"type": "integer",
                                  "description": "每处命中前后各显示几行（0–5，默认 0）"},
                      "max_per_file": {"type": "integer",
                                       "description": "每个文件最多列几处（默认 5）"},
                      "count_only": {"type": "boolean",
                                     "description": "true = 只统计每个文件命中几处，不返回原文"}},
                     ["pattern"]),
            _fn_tool("search_code", "语义检索代码：用自然语言描述要找的逻辑（不记得函数名时用）",
                     {"query": {"type": "string",
                                "description": "自然语言描述，例如「工具步骤组如何折叠」"},
                      "limit": {"type": "integer", "description": "返回几段，默认 6"}},
                     ["query"]),
            _fn_tool("repo_map", "仓库结构图：按文件列出顶层函数/类与行号（先看骨架再读文件）",
                     {"path": {"type": "string",
                               "description": "可选：只看某个目录或文件"},
                      "max_files": {"type": "integer",
                                    "description": "最多列多少个文件，默认 60"}},
                     []),
            _fn_tool("write_file",
                     "覆盖或创建整个文件。覆盖已有文件前必须先 read_file；"
                     "若返回 READ_REQUIRED / STALE_VERSION，按「恢复」提示重读后重试",
                     {"path": {"type": "string"}, "content": {"type": "string"}},
                     ["path", "content"]),
            _fn_tool("edit_file",
                     "精确替换文件中的一段文本（old 必须唯一）。"
                     "编辑已有文件前必须先 read_file；old 不唯一时给出更长的片段",
                     {"path": {"type": "string"}, "old": {"type": "string"},
                      "new": {"type": "string"}},
                     ["path", "old", "new"]),
            _fn_tool("make_dir", "创建目录", {"path": {"type": "string"}}, ["path"]),
            _fn_tool("move_file", "重命名或移动文件",
                     {"from": {"type": "string"}, "to": {"type": "string"}},
                     ["from", "to"]),
            _fn_tool("copy_file", "复制文件（沙箱内）；目标已存在时需要 overwrite=true",
                     {"from": {"type": "string"}, "to": {"type": "string"},
                      "overwrite": {"type": "boolean",
                                    "description": "目标已存在时是否覆盖，默认 false"}},
                     ["from", "to"]),
            _fn_tool("delete_file", "删除文件或空目录",
                     {"path": {"type": "string"}}, ["path"]),
            _fn_tool("run_command",
                     "在沙箱内执行一条命令并拿到输出（构建 / 测试 / 脚本 / git 均可，"
                     "例如 pytest、npm test、go test ./...、cargo build）。"
                     "不支持管道、重定向、后台常驻进程；也不支持 python -c / node -e 内联代码，"
                     "代码请先写成沙箱内的脚本文件",
                     {"argv": {"type": "array", "items": {"type": "string"},
                               "description": "命令与参数，如 [\"python\",\"-m\",\"pytest\",\"-q\"]"},
                      "cwd": {"type": "string", "description": "工作目录，沙箱内相对路径，默认沙箱根"},
                      "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"}},
                     ["argv"]),
            _fn_tool("shell",
                     "持久终端（PowerShell）：同一会话内 cd / 环境变量 / 后台进程都会保留。"
                     "适合管道、交互式命令、起 dev server 后再继续操作；"
                     "一次性命令优先用 run_command（更快、输出更干净）。"
                     "run 在 wait 秒内收集输出；命令还没结束就用 read 继续取，"
                     "interrupt 发 Ctrl+C，close 关闭终端",
                     {"action": {"type": "string",
                                 "enum": ["run", "read", "interrupt", "close"],
                                 "description": "默认 run"},
                      "command": {"type": "string", "description": "run 时必填，整条命令行（可含管道）"},
                      "wait": {"type": "integer", "description": "run 等待输出秒数，默认 8，最大 60"}},
                     ["action"]),
            _fn_tool("render_check",
                     "用无头浏览器真的打开一个 HTML，回传运行时错误与布局问题"
                     "（溢出、遮挡、字号过小、对比度、零尺寸画布、点击区过小）。"
                     "网页 / canvas / UI / 小游戏写完后用它自检，不要凭想象判断画面",
                     {"path": {"type": "string", "description": "沙箱内的 .html / .htm"},
                      "viewport": {"type": "string",
                                   "description": "视口，如 1280x800 或 375x667（手机），默认 1280x800"},
                      "screenshot": {"type": "boolean",
                                     "description": "true 时额外截图并让视觉模型描述画面；"
                                                    "更慢、更贵，只在需要判断观感时开"}},
                     ["path"]),
            _fn_tool("visual_check",
                     "视觉 QA：无头浏览器打开 HTML，回传控制台/资源错误、WebGL canvas 与 draw calls、"
                     "三角面 / 着色器 / 绑定纹理、帧率、亮度/对比度/清晰度/边缘密度、截图，"
                     "并可与基线截图比 SSIM。"
                     "做游戏 / 动画 / 3D 页面时用它量化「画面糊不糊、卡不卡、几何量对不对」",
                     {"path": {"type": "string", "description": "沙箱内的 .html / .htm"},
                      "viewport": {"type": "string", "description": "视口，如 1280x720，默认 1280x720"},
                      "viewports": {"type": "array", "items": {"type": "string"},
                                    "description": "可选：最多 3 个视口，如 [\"1280x720\",\"390x844\"]"},
                      "screenshot": {"type": "boolean", "description": "是否截图，默认 true"},
                      "describe": {"type": "boolean",
                                   "description": "true 时让视觉模型描述截图，更慢更贵"},
                      "baseline": {"type": "string",
                                   "description": "基线名；有基线图时回传 SSIM"},
                      "save_baseline": {"type": "boolean",
                                        "description": "true 时把本次截图保存为基线"}},
                     ["path"]),
            _fn_tool("render_views",
                     "多视图截图：无头浏览器按 views 的每个视角打开同一个页面并截图。"
                     "页面需要读取 location.search 里的 view 参数来设置相机（如 ?view=front）；"
                     "没适配时会提示「所有截图相同」。产物落在 data/visual 下并生成 manifest.json，"
                     "供 geometry_compare 对照",
                     {"path": {"type": "string", "description": "沙箱内的 .html / .htm"},
                      "views": {"type": "array",
                                "description": "视角列表：字符串（front / side / top / iso）或 "
                                               "{name, query}；默认 4 个标准视角"},
                      "viewport": {"type": "string", "description": "视口，默认 1280x720"},
                      "timeout": {"type": "integer", "description": "单张截图超时秒数，默认 90"}},
                     ["path"]),
            _fn_tool("geometry_compare",
                     "多视图几何对照：把参考图与产物图按视角配对，逐视图算轮廓 IoU / SSIM / "
                     "主色 / 比例差与 0–100 分，再给平均分与最差视图。"
                     "用来发现单视角看不出的比例 / 朝向 / 穿插问题；只做像素形状统计，"
                     "不判断语义与美观",
                     {"reference": {"type": "array", "items": {"type": "string"},
                                    "description": "参考图路径（与 candidate 按顺序配对）"},
                      "candidate": {"type": "array", "items": {"type": "string"},
                                    "description": "产物图路径（与 reference 按顺序配对）"},
                      "names": {"type": "array", "items": {"type": "string"},
                                "description": "可选：每个视图的名字（front / side…）"},
                      "region": {"type": "array", "items": {"type": "number"},
                                 "description": "可选：所有视图共用的裁剪区域 [x, y, w, h]（0–1）"},
                      "max_side": {"type": "integer", "description": "比较前缩放的最长边，默认 1024"},
                      "min_score": {"type": "number",
                                    "description": "及格分，低于它的视图计入 views_failed，默认 70"}},
                     ["reference", "candidate"]),
            _fn_tool("scene_assert",
                     "场景断言：页面暴露 window.__VQA_SCENE_INFO__（对象 / 相机 / 灯光 / renderer）"
                     "后，按断言检查对象是否存在、数量、隐藏数、相机 FOV、灯光数、三角面、整体包围盒。"
                     "没暴露钩子时返回可直接照抄的示例；只读场景事实，不渲染",
                     {"path": {"type": "string", "description": "沙箱内的 .html / .htm"},
                      "assertions": {"type": "array",
                                     "description": "断言列表，如 "
                                                    '[{"kind":"exists","name":"Ground"},'
                                                    '{"kind":"lights_min","value":1},'
                                                    '{"kind":"bbox_y_max","value":2}]',
                                     "items": {"type": "object"}},
                      "viewport": {"type": "string", "description": "视口，默认 1280x720"},
                      "timeout": {"type": "integer", "description": "超时秒数，默认 90"}},
                     ["path", "assertions"]),
            _fn_tool("asset_scan",
                     "扫描素材目录：图片尺寸/格式/哈希/重复、音频、模型、总大小；"
                     "可把清单写成 JSON，供 asset_validate 使用",
                     {"path": {"type": "string", "description": "沙箱内目录，默认沙箱根"},
                      "out": {"type": "string", "description": "可选：清单输出路径，如 assets.json"}},
                     []),
            _fn_tool("asset_convert",
                     "图片转换/缩放：png / jpeg / webp，可指定宽高与质量",
                     {"from": {"type": "string"}, "to": {"type": "string"},
                      "format": {"type": "string", "description": "png / jpeg / webp；默认按 to 后缀"},
                      "width": {"type": "integer"}, "height": {"type": "integer"},
                      "quality": {"type": "integer", "description": "jpeg/webp 质量，默认 85"}},
                     ["from", "to"]),
            _fn_tool("asset_atlas",
                     "把多张图片打包成图集，并生成 JSON 帧映射；带 padding，避免采样出血",
                     {"images": {"type": "array", "items": {"type": "string"}},
                      "out": {"type": "string", "description": "图集 PNG 路径"},
                      "map": {"type": "string", "description": "可选：JSON 映射路径"},
                      "max_size": {"type": "integer", "description": "图集边长，默认 2048"},
                      "padding": {"type": "integer", "description": "每帧外边距，默认 2"}},
                     ["images", "out"]),
            _fn_tool("asset_inline",
                     "把素材转成 base64 data URI，写成 JSON 或 JS（window.__ASSETS__）；"
                     "适合素材在沙箱外、或部署时不想带独立文件",
                     {"images": {"type": "array", "items": {"type": "string"}},
                      "out": {"type": "string", "description": "输出路径，.js 或 .json"},
                      "max_kb": {"type": "integer", "description": "可选：内联体积上限（KB）"}},
                     ["images", "out"]),
            _fn_tool("asset_validate",
                     "按素材清单检查：文件是否存在、图片尺寸是否变化、图集文件是否缺失",
                     {"manifest": {"type": "string", "description": "清单 JSON，默认 assets.json"}},
                     []),
            _fn_tool("mesh_inspect",
                     "3D 模型体检（glTF / GLB / OBJ）：解析节点 / mesh / primitive、三角面与顶点数、"
                     "世界包围盒、材质与纹理引用、法线 / UV 缺失、贴图文件是否存在；"
                     "不渲染、不判断好不好看，也不检查非流形 / 自交。"
                     "做完 3D 模型后先跑它确认「几何真的建出来了」，再截图验画面",
                     {"path": {"type": "string", "description": "沙箱内的 .gltf / .glb / .obj"},
                      "units": {"type": "string",
                                "description": "可选：模型单位说明（如 cm / m），只用于回报"}},
                     ["path"]),
            _fn_tool("audio_probe",
                     "真的读一遍音频文件（WAV/FLAC/OGG/MP3），回传客观声学指标："
                     "响度包络、起音时间、RT60 混响、频谱形状、事件密度，"
                     "外加「冲击/压迫/空间/清晰」四个代理分数。"
                     "写完或改完音效后用它自检，不要凭想象描述声音；"
                     "它**判断不了好不好听**（那需要人耳）",
                     {"path": {"type": "string", "description": "沙箱内的音频文件"}},
                     ["path"]),
            _fn_tool("audio_compare",
                     "对比两个音频（改前 vs 改后）：先抵消整体响度差，再逐项报告"
                     "结构差异（混响、频谱、事件密度）与包络形状相关度",
                     {"a": {"type": "string", "description": "基准音频（改前）"},
                      "b": {"type": "string", "description": "对比音频（改后）"}},
                     ["a", "b"]),
            _fn_tool("see_image",
                     "真的看一眼图片（PNG/JPG/WEBP/GIF/BMP），回传视觉模型对它内容的文字描述。"
                     "写完图 / 下载完图片后用它确认「实际长什么样」，不要凭想象描述；"
                     "需要已配置视觉模型或聊天 API 密钥。"
                     "先要客观数据（是不是空白图、主体 bbox、主色、多视图面板）时先用 image_analyze，"
                     "它不消耗视觉模型调用。"
                     "可以用 paths 一次传多张（最多 %d 张）做同请求对照，"
                     "回答里用「第 N 张」区分" % SEE_MAX_IMAGES,
                     {"path": {"type": "string", "description": "沙箱内的单张图片"},
                      "paths": {"type": "array", "items": {"type": "string"},
                                "description": "可选：多张图片（与 path 合并，最多 %d 张）"
                                               % SEE_MAX_IMAGES},
                      "question": {"type": "string",
                                   "description": "可选：重点看什么 / 要比什么"},
                      "native": {"type": "boolean",
                                 "description": "默认 true：把图直接附到本轮上下文让模型看；"
                                                "false：只返回文字描述（省 token）"}},
                     []),
            _fn_tool("attachment_read",
                     "回看本会话里用户之前发过的图片 / 文档附件（历史消息里会给出 att_xxx 编号）。"
                     "用户说「刚才那张图 / 之前那个文件」时用它，不要让用户重发。"
                     "图片会重新识图（原生视觉可用时直接附到本轮上下文）；"
                     "文档返回发送时抽取的正文，可用 offset / limit 分段读。"
                     "不传 id / name 时列出本会话全部附件",
                     {"id": {"type": "string", "description": "附件编号（att_xxx）"},
                      "name": {"type": "string", "description": "可选：按文件名查找（id 优先）"},
                      "question": {"type": "string",
                                   "description": "可选：看图时重点看什么"},
                      "offset": {"type": "integer",
                                 "description": "文档正文起始字符，默认 0"},
                      "limit": {"type": "integer",
                                "description": "文档这次读多少字，默认 6000，最大 20000"}},
                     []),
            _fn_tool("image_compare",
                     "对照两张图（参考物 vs 产物），输出客观指标：轮廓重合 IoU、宽高比差、"
                     "结构相似 SSIM、主色分布差、边缘密度差、3×3 局部最大差与总分 0–100，"
                     "并列出差距最大的三项。它只做像素 / 形状统计，不理解语义、不判断好不好看；"
                     "用于「还原得像不像」的机检与迭代排序。背景尽量干净，"
                     "有地面 / 阴影 / 图注时用 region 裁到主体或局部",
                     {"reference": {"type": "string", "description": "参考图（沙箱内路径）"},
                      "candidate": {"type": "string", "description": "产物图（沙箱内路径）"},
                      "region": {"type": "array", "items": {"type": "number"},
                                 "description": "可选：两张图共用的裁剪区域 [x, y, w, h]，0–1 归一化"},
                      "max_side": {"type": "integer",
                                   "description": "可选：比较前缩放的最长边，默认 1024"}},
                     ["reference", "candidate"]),
            _fn_tool("image_analyze",
                     "测量一张图的客观几何：背景色、前景占比、主体 bbox、宽高比、主色、"
                     "行 / 列剖面、ASCII 剪影，并自动切分多视图面板（返回归一化 region，"
                     "可直接交给 image_compare 逐面板对照）。只做图像统计，不理解内容；"
                     "面板切分是启发式，仅供对照参考。"
                     "渲染 / 截图落盘后先跑它：blank_score 能直接判断是不是白图，"
                     "不用花一次视觉模型调用去「看一眼」",
                     {"path": {"type": "string", "description": "沙箱内的图片路径"},
                      "region": {"type": "array", "items": {"type": "number"},
                                 "description": "可选：只分析这块区域 [x, y, w, h]，0–1 归一化"},
                      "panels": {"type": "string",
                                 "description": "面板切分：auto（启发式）/ none / "
                                                "显式网格如 2x2、1x3（多视图最稳）"},
                      "max_side": {"type": "integer",
                                   "description": "可选：分析前缩放的最长边，默认 1024"}},
                     ["path"]),
            _fn_tool("read_tool_result",
                     "精读或搜索一条被截断 / 已落盘的工具结果：结果里会给出 handle（r2、sp_1a2b3c4d5e）。"
                     "已知位置用 start/end 取字符区间；不知道在哪就先给 query 搜索"
                     "（按行匹配，返回行号 + 绝对偏移 + 上下文），再用 start=<偏移> 精读。"
                     "sp_ 开头的落盘句柄跨轮、跨重启都有效；不要重新跑工具。",
                     {"handle": {"type": "string", "description": "截断提示里给出的句柄"},
                      "start": {"type": "integer", "description": "起始字符（0 起），默认 0"},
                      "end": {"type": "integer", "description": "结束字符（不含），默认 +12000"},
                      "query": {"type": "string",
                                "description": "可选：在结果里搜索的关键词；给了它就走搜索模式"},
                      "mode": {"type": "string", "enum": ["literal", "regex"],
                               "description": "搜索方式，默认 literal（正则按行匹配，避免回溯炸掉）"},
                      "ignore_case": {"type": "boolean",
                                      "description": "搜索时忽略大小写，默认 false"},
                      "context": {"type": "integer",
                                  "description": "每条命中带几行上下文（0–5），默认 2"},
                      "max_hits": {"type": "integer",
                                   "description": "最多返回几处命中，默认 20（上限 50）"}},
                     ["handle"]),
            _fn_tool("find_symbol",
                     "按符号名找定义与引用（沙箱内 .py/.js/.ts）。"
                     "不记得代码在哪个文件、或想知道谁在调用它时用这个；返回 文件:行号 + 代码行",
                     {"name": {"type": "string", "description": "函数 / 类 / 变量名（精确匹配）"},
                      "path": {"type": "string", "description": "可选：限定目录或文件"}},
                     ["name"]),
            _fn_tool("code_context",
                     "一次拿到代码上下文包：定义（签名 + 文档首行）+ 直接引用 + 相关测试 + 语义片段。"
                     "找实现、问「这个功能在哪 / 怎么做的」时**优先用这个**，比先 search_code 再 "
                     "read_file 少好几轮工具调用。知道符号名就填 symbol，只知道描述就填 query",
                     {"query": {"type": "string",
                                "description": "自然语言描述，如「工具步骤组怎么折叠」；也可直接填符号名"},
                      "symbol": {"type": "string",
                                 "description": "可选：确切的函数 / 类名（给了会做精确符号查询）"},
                      "path": {"type": "string",
                               "description": "可选：限定目录或文件，缩小范围"}},
                     ["query"]),
            _fn_tool("impact",
                     "变更影响分析：改这个函数 / 文件会影响谁。返回定义、调用者、其他引用，"
                     "以及最可能需要重跑的测试文件。改代码前用它，比 grep 猜更准",
                     {"target": {"type": "string",
                                 "description": "符号名（如 load_config）或文件相对路径（如 app/agent.py）"},
                      "path": {"type": "string",
                               "description": "可选：符号重名时限定目录"}},
                     ["target"]),
            _fn_tool("list_steps",
                     "列出本次会话的写操作步骤（token / 文件 / 是否已撤销）。"
                     "想知道「刚才改了哪几步」或要回退某一步时先用它",
                     {}, []),
            _fn_tool("undo_step",
                     "撤销某一步写操作（token 从 list_steps 拿）。"
                     "比整轮回滚更细：只回退一步，后面的改动保留",
                     {"token": {"type": "string", "description": "list_steps 给出的步骤 id"}},
                     ["token"]),
            _fn_tool("redo_step",
                     "重做某一步被撤销的写操作（token 从 list_steps 拿）。"
                     "删除 / 移动类操作没有写后快照，无法重做",
                     {"token": {"type": "string", "description": "list_steps 给出的步骤 id"}},
                     ["token"]),
            _fn_tool("recover_file",
                     "从本会话的写后快照恢复丢失 / 被外部删除的文件。"
                     "文件不见了先用 list_steps 看步骤，再用这个恢复；"
                     "不传 path/token 时只列出可恢复文件。默认不覆盖已存在且内容不同的文件",
                     {"path": {"type": "string",
                               "description": "要恢复的沙箱相对路径；文件精确匹配，目录按前缀批量匹配。留空则列出可恢复文件"},
                      "token": {"type": "string",
                                "description": "可选：恢复某一步写操作涉及的全部文件"},
                      "force": {"type": "boolean",
                                "description": "文件已存在且内容不同时是否覆盖，默认 false"},
                      "dry_run": {"type": "boolean",
                                  "description": "只列出将要恢复的文件，不写盘"}},
                     []),
            _fn_tool("check_code",
                     "语法体检：对文件或整个目录跑 .py/.js/.json 检查（与写后自检同一套规则），"
                     "返回所有报错位置。**不做类型检查**（那需要 pyright / mypy）",
                     {"path": {"type": "string", "description": "文件或目录，默认沙箱根"}},
                     []),
            _fn_tool("git_status",
                     "查看 Git 状态：当前分支、领先/落后、改动文件（已暂存与未暂存）",
                     {}, []),
            _fn_tool("git_diff",
                     "看某个文件相对 Git 的改动（unified diff）。"
                     "改完后想确认「到底改了什么」用它，不要凭记忆复述",
                     {"path": {"type": "string", "description": "沙箱内的相对路径"},
                      "staged": {"type": "boolean",
                                 "description": "true = 看暂存区相对 HEAD 的改动"}},
                     ["path"]),
            _fn_tool("git_log",
                     "看最近的提交记录（hash / 日期 / 作者 / 标题）",
                     {"limit": {"type": "integer", "description": "条数，默认 20"}},
                     []),
        ]
        if lsp_on:
            out += [
                _fn_tool("lsp_diagnostics",
                         "用 pyright 做**类型检查**（不只是语法）：找类型不匹配、参数错、"
                         "属性不存在。改完代码想确认「真的没问题」时用它；"
                         "查语法用 check_code，查类型用这个",
                         {"path": {"type": "string",
                                   "description": "文件或目录；目录最多看 60 个 .py"}},
                         []),
                _fn_tool("lsp_definition",
                         "跳到符号的定义（真正的语义跳转，跨文件、认导入别名）。"
                         "只有「知道文件和行号、想知道它定义在哪」时用它；"
                         "不知道在哪用 find_symbol / search_code",
                         {"path": {"type": "string", "description": "当前文件（相对路径）"},
                          "line": {"type": "integer", "description": "符号所在行，从 1 开始"},
                          "symbol": {"type": "string",
                                     "description": "该行的符号名（给了才定位得准）"}},
                         ["path", "line"]),
                _fn_tool("lsp_references",
                         "找符号的所有引用（认作用域，不会把同名变量混进来）",
                         {"path": {"type": "string", "description": "当前文件（相对路径）"},
                          "line": {"type": "integer", "description": "符号所在行，从 1 开始"},
                          "symbol": {"type": "string", "description": "该行的符号名"}},
                         ["path", "line"]),
                _fn_tool("lsp_rename",
                         "语义级重命名：一次改完所有文件里的定义与引用（会走写入审批）。"
                         "比手工 edit_file 逐处改安全，改名优先用它",
                         {"path": {"type": "string", "description": "符号所在文件（相对路径）"},
                          "line": {"type": "integer", "description": "符号所在行，从 1 开始"},
                          "symbol": {"type": "string", "description": "要改名的符号"},
                          "new_name": {"type": "string", "description": "新名字"}},
                         ["path", "line", "symbol", "new_name"]),
                _fn_tool("lsp_hover",
                         "看某个符号的类型 / 签名 / 文档（真正的语义信息）。"
                         "想知道「这个函数收什么、返回什么」时用它，比读整个文件省 token",
                         {"path": {"type": "string", "description": "文件（相对路径）"},
                          "line": {"type": "integer", "description": "符号所在行，从 1 开始"},
                          "symbol": {"type": "string",
                                     "description": "该行的符号名（给了才定位得准）"}},
                         ["path", "line"]),
                _fn_tool("lsp_symbols",
                         "符号浏览：给 path 列该文件的符号树（能到方法级）；"
                         "不给 path、只给 query 则全项目按名字搜符号（比 grep 准，不混注释和字符串）",
                         {"path": {"type": "string", "description": "可选：只看这个文件"},
                          "query": {"type": "string",
                                    "description": "可选：全项目符号名片段（不给 path 时用）"}},
                         []),
                _fn_tool("lsp_implementation",
                         "找某个接口 / 抽象方法的所有实现（改接口前先看谁实现了它）",
                         {"path": {"type": "string", "description": "文件（相对路径）"},
                          "line": {"type": "integer", "description": "符号所在行，从 1 开始"},
                          "symbol": {"type": "string", "description": "该行的符号名"}},
                         ["path", "line"]),
                _fn_tool("lsp_calls",
                         "调用链：incoming = 谁调用了它，outgoing = 它调用了谁。"
                         "改函数签名前用它确认影响面，比 grep 准",
                         {"path": {"type": "string", "description": "文件（相对路径）"},
                          "line": {"type": "integer", "description": "符号所在行，从 1 开始"},
                          "symbol": {"type": "string", "description": "该行的符号名"},
                          "direction": {"type": "string",
                                        "description": "incoming（默认，调用者）| outgoing（下游）"}},
                         ["path", "line"]),
            ]
        if str(git_commit_mode or "off") != "off":
            out.append(_fn_tool(
                "git_commit",
                "提交已暂存的改动。没有暂存任何东西时可用 paths / all 指定；"
                "提交信息写「为什么改」，不要写「修改了某文件」",
                {"message": {"type": "string", "description": "提交信息（一句话说清动机）"},
                 "paths": {"type": "array", "items": {"type": "string"},
                           "description": "可选：只提交这些路径（会先暂存）"},
                 "all": {"type": "boolean",
                         "description": "true = 先 git add -A 再提交"}},
                ["message"]))
            out.append(_fn_tool(
                "split_commit",
                "按逻辑分组提交：一个任务改了多个模块时，用几次语义清晰的提交代替一次「改了很多」。"
                "每组会先跑该组相关测试，失败就停在那组之前。文件不能重复出现在多组",
                {"groups": {"type": "array",
                            "description": "分组列表，按提交顺序",
                            "items": {"type": "object",
                                      "properties": {
                                          "message": {"type": "string",
                                                      "description": "这一组的提交信息（写「为什么」）"},
                                          "paths": {"type": "array",
                                                    "items": {"type": "string"},
                                                    "description": "这一组包含的文件（相对路径）"}},
                                      "required": ["message", "paths"]}}},
                ["groups"]))
    # archify（流程图 / 架构图 / 时序图…）只在 Agent 模式下提供：
    # 它要跑 validate / 浏览器检查，还要有可写的工作目录；非 Agent 会话没有沙箱，
    # 产物也没地方放。非 Agent 需要配图时走内联围栏（svg / mermaid / chart）。
    # 仍然受工具分层约束：allow_groups 不含 diagram 时由 hidden_tools 摘掉，
    # 命中「流程图 / 架构图…」或 tool_search 解锁后才会出现。
    if agent_on:
        out.append(_fn_tool(
            "archify",
            "先把系统 / 流程 / 计划 / 代码库画成可验证的交互式 HTML：架构、工作流、时序、"
            "数据流、状态、ERD、类图、树、时间线、瀑布图，或『一页式解释页』。"
            "图内文字（标题 / 节点 / 标签 / 说明）默认用中文；Viewer 界面也会自动用中文。"
            "先用 action=schema 取对应 schema + 示例；写候选（大候选先 write_file，"
            "再传 candidate_path）；action=finalize 会跑 validate → 原子写入 → "
            "严格检查 → 真实浏览器检查，全过才交付，产物会直接显示在对话里；"
            "action=doctor 检查环境。",
            {"action": {"type": "string",
                        "enum": ["finalize", "schema", "doctor"],
                        "description": "默认 finalize"},
             "type": {"type": "string",
                      "enum": ["architecture", "workflow", "sequence", "dataflow",
                               "lifecycle", "erd", "tree", "class", "timeline",
                               "waterfall", "explainer"],
                      "description": "图型；explainer 的候选是 Markdown，其余是 JSON"},
             "candidate": {"type": "string",
                           "description": "候选内容（JSON；explainer 为 Markdown）。"
                                          "大候选请改用 candidate_path"},
             "candidate_path": {"type": "string",
                                "description": "工作目录内的候选文件；优先用这个"
                                               "（Agent 模式下是沙箱相对路径）"},
             "output": {"type": "string",
                        "description": "HTML 输出路径；省略则写 "
                                       ".archify/<type>-<时间>/<type>.html"},
             "quality": {"type": "string", "enum": ["showcase", "standard"],
                         "description": "默认 showcase"},
             "repo_root": {"type": "string",
                           "description": "可选：代码库根目录（画『从代码得图』时用）"},
             "timeout": {"type": "integer",
                         "description": "超时秒数，默认 180，最大 600"},
             "example": {"type": "boolean",
                         "description": "schema 时是否附一份官方示例，默认 true"}},
            ["action"]))
    if search_ready:
        out.append(_fn_tool(
            "web_search",
            "联网搜索：1–3 个关键词式检索式。复刻/还原/规格类任务按缺口分轮补搜"
            "（每轮 1–3 个），不要一次搜完就写；结果里的 [n] 是台账全局编号",
            {"queries": {"type": "array", "items": {"type": "string"}}},
            ["queries"]))
    if computer_on:
        out += [
            _fn_tool("computer_open",
                     "需要登录、点击、填表、提交表单、读取登录后才能看的内容、或验证真实页面时，"
                     "在受控浏览器打开目标页面；普通查资料优先用 web_search / fetch_url。"
                     "未授权的站点会先让用户确认",
                     {"url": {"type": "string", "description": "完整 URL，如 https://example.com"}},
                     ["url"]),
            _fn_tool("computer_see",
                     "打开页面后、页面跳转后、或需要确认当前状态时看页面：可交互元素带编号，"
                     "后续用编号操作（不要猜坐标）。target 给窗口编号（如 w1）时看桌面程序",
                     {"target": {"type": "string",
                                 "description": "省略 = 浏览器；w1 这类编号 = 桌面窗口"},
                      "start": {"type": "integer", "description": "从第几个元素开始，默认 0"},
                      "limit": {"type": "integer", "description": "本次取几个，默认 30"}},
                     []),
            _fn_tool("computer_act",
                     "对编号元素执行动作：click / type / press / scroll / hover；"
                     "提交、支付、删除等高危按钮会先请用户确认",
                     {"action": {"type": "string",
                                 "enum": ["click", "type", "press", "scroll", "hover"]},
                      "ref": {"type": "integer", "description": "computer_see 给出的编号"},
                      "target": {"type": "string", "description": "桌面窗口编号，如 w1；省略 = 浏览器"},
                      "value": {"type": "string",
                                "description": "type 的文本 / press 的按键名 / scroll 的像素数"},
                      "clear": {"type": "boolean", "description": "type 前先清空输入框"}},
                     ["action"]),
            _fn_tool("computer_read",
                     "页面报错、要确认当前地址、读取某个输入框的值或回看动作记录时用："
                     "console（错误/警告）、network（最近请求）、url、value、trace",
                     {"kind": {"type": "string",
                               "enum": ["console", "network", "url", "value", "trace"]},
                      "ref": {"type": "integer", "description": "kind=value 时需要"},
                      "target": {"type": "string", "description": "桌面窗口编号，如 w1"}},
                     []),
            _fn_tool("computer_shot",
                     "判断配色 / 布局 / 画布类页面观感时截图并让视觉模型描述；"
                     "成本较高，结构化信息够用时不要调",
                     {"target": {"type": "string", "description": "桌面窗口编号，如 w1；省略 = 浏览器"}},
                     []),
            _fn_tool("computer_windows",
                     "列出允许操作的桌面窗口（只有设置里的白名单程序会出现）",
                     {}, []),
        ]
    out.append(_fn_tool(
        "set_reminder", "设置单次提醒（at 用绝对本地时间 YYYY-MM-DD HH:MM）",
        {"at": {"type": "string"}, "text": {"type": "string"},
         "repeat": {"type": "string", "enum": ["", "daily", "weekly"]}},
        ["at", "text"]))
    out.append(_fn_tool(
        "ask_user",
        "向用户提问；一次可问多个问题；允许多选的问题把 multiple 设为 true。"
        "每个选项建议写成 {label, desc, recommended}：label 是一行标题，"
        "desc 用一句话说明代价 / 影响（做方案对比时很有用），recommended 标出你推荐的那个。"
        "只传字符串也兼容，但没有说明与推荐。"
        "如果这件事必须由用户在你够不到的地方**亲手完成**（登录 / 扫码 / 装依赖 / "
        "点系统弹窗 / 插设备），把 action 设为 true，前端会弹「等待你操作」卡，"
        "用户做完点「我已完成」你再继续",
        {"questions": {"type": "array", "items": {
            "type": "object",
            "properties": {"question": {"type": "string"},
                           "options": {"type": "array", "items": {
                               "type": "object",
                               "properties": {
                                   "label": {"type": "string",
                                             "description": "选项标题（一行）"},
                                   "desc": {"type": "string",
                                            "description": "这个方案的代价 / 影响，一句话"},
                                   "recommended": {"type": "boolean",
                                                   "description": "是否推荐这一项"}},
                               "required": ["label"]}},
                           "multiple": {"type": "boolean",
                                        "description": "该题是否允许多选；题目会自动标注（多选）"},
                           "action": {"type": "boolean",
                                      "description": "true = 需要用户亲手操作的一步（不是选择题）"},
                           "detail": {"type": "string",
                                      "description": "action=true 时的一句话说明：为什么要你来做"},
                           "items": {"type": "array", "items": {"type": "string"},
                                     "description": "action=true 时的操作清单，一步一条"}},
            "required": ["question"]}}},
        ["questions"]))
    out.append(_fn_tool(
        "fetch_url",
        "抓取一个 http(s) 页面的可读正文（文档 / issue / 报错页 / 单篇文章）。"
        "只做只读 GET，拒绝本机与内网地址；需要按关键词找多个来源时用 web_search",
        {"url": {"type": "string"},
         "query": {"type": "string", "description": "可选：只取与它相关的段落"},
         "chars": {"type": "integer", "description": "正文上限，默认 12000 字"}},
        ["url"]))
    out.append(_fn_tool(
        "sleep", "等待若干秒（最多 30），用于等端口 / 服务启动；不要用它代替真正的等待条件",
        {"seconds": {"type": "number"}}, ["seconds"]))
    out.append(_fn_tool(
        "research_note",
        "研究台账：登记 / 关闭「还不知道什么」，以及已核实的结论（跨轮保留，"
        "不会因为上下文裁剪而丢失）。复刻 / 还原类任务动笔前先用它列缺口。",
        {"action": {"type": "string",
                    "enum": ["gap_add", "gap_done", "claim_add", "status", "clear"],
                    "description": "gap_add 登记缺口；gap_done 关闭缺口；"
                                   "claim_add 记结论；status 查看台账；clear 清空"},
         "gaps": {"type": "array", "items": {"type": "string"},
                  "description": "action=gap_add：还缺哪些信息（一条一个）"},
         "ids": {"type": "array", "items": {"type": "string"},
                 "description": "action=gap_done：要关闭的缺口编号，如 g1"},
         "claims": {"type": "array", "items": {
             "type": "object",
             "properties": {"text": {"type": "string", "description": "已核实的结论"},
                            "sources": {"type": "array", "items": {"type": "integer"},
                                        "description": "来源编号（台账里的 [n]）"}},
             "required": ["text"]},
             "description": "action=claim_add：结论 + 来源编号"}},
        ["action"]))
    out.append(_fn_tool(
        "explore",
        "派出侦察兵并行查清问题：入口不明、跨多个模块、需要交叉验证时用。"
        "子代理会分叉父会话上下文（受预算限制），不用重复解释背景。"
        "最多 5 个问题；每个子代理只回传 600 字以内的结论与「文件:行号」，"
        "系统会重新读取核对引用，引用不存在会重试一次。"
        "单文件问题不要用，直接 read_file / search_code。"
        "任务包含多个独立问题 / 模块时优先用它并行，而不是在主循环里串行逐个查。"
        "结论互相矛盾或你不确定时，把 verify 设为 true，会追加一个反证者核对证据。"
        "要改代码时设 allow_write=true：子代理可以写沙箱内文件，每次写仍会弹审批。"
        "要查外部资料（规格 / 机制 / 参数 / 参考实现）时设 research=true："
        "子代理会带联网搜索，只回结论 + 来源 URL，不碰本地代码。",
        {"goal": {"type": "string", "description": "这一批探索要查清什么"},
         "questions": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "ask": {"type": "string", "description": "要查清的具体问题"},
                 "scope": {"type": "array", "items": {"type": "string"},
                           "description": "优先搜索的目录 / 路径前缀"},
                 "hint_files": {"type": "array", "items": {"type": "string"},
                                "description": "已知线索文件（让子代理优先看这些）"}},
             "required": ["ask"]}},
         "deliverable": {"type": "string",
                         "description": "交付形态，如「调用链」「改动建议」「文件清单」"},
         "acceptance": {"type": "array", "items": {"type": "string"},
                        "description": "可机检验收条件：cites>=2（引用文件数）/ answers>=1 / "
                                       "sources>=2（引用来源 URL 数）/ cross>=2（至少 2 个"
                                       "不同站点互相印证）/ contains:文本 / not_contains:文本 / "
                                       "evidence>=0.8"},
         "allow_write": {"type": "boolean",
                         "description": "允许子代理在沙箱内写文件（每次写仍走审批）"},
         "research": {"type": "boolean",
                      "description": "外部资料调研模式：子代理只带 web_search，"
                                     "结论必须带来源 URL（与本批的 allow_write 互斥）"},
         "verify": {"type": "boolean",
                    "description": "追加反证者核对结论与证据（冲突 / 不确定时用）"},
         "background": {"type": "boolean",
                        "description": "后台运行：立即返回批次号，不阻塞当前回合；"
                                       "之后用 explore_status / explore_result 取"}},
        ["goal", "questions"]))
    _delegate_desc = (
        "把任务派给可配置的子代理团队。默认有 researcher（只联网）、"
        "reviewer（只读审查）、critic（反证）、implementer（可写，每次写仍走审批）；"
        "用户可以在「设置 → 子代理」里新建或修改代理，指定工具、模型与写权限。"
        "多个独立任务放进 agents 数组会并行执行；需要串行交接时用 pipeline。"
        "单次并行数量由设置里的 agent_explore_max 决定（默认 8，硬上限 12）。"
        "每个代理只回传 600 字以内结论与证据。")
    try:
        from app import agent_defs as _agent_defs
        _rows = [a for a in _agent_defs.list_agents()
                 if a.get("enabled") and not a.get("error")]
        if _rows:
            _delegate_desc += " 当前可用：" + "；".join(
                "%s（%s）" % (a["id"], (a.get("description") or a.get("name") or "")[:40])
                for a in _rows[:12]) + "。"
    except Exception:                        # noqa: BLE001  定义读不到不影响工具可用
        pass
    out.append(_fn_tool(
        "delegate",
        _delegate_desc,
        {"goal": {"type": "string", "description": "这一批派活要解决什么"},
         "agents": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "agent": {"type": "string",
                           "description": "代理 id，如 researcher / reviewer / critic / implementer"},
                 "task": {"type": "string", "description": "交给这个代理的具体任务"}},
             "required": ["agent", "task"]}},
         "pipeline": {"type": "array", "items": {"type": "string"},
                      "description": "串行交接的代理 id 列表，如 "
                                     "[planner, implementer, reviewer]；"
                                     "上一阶段输出会作为下一阶段背景，与 agents 二选一"},
         "task": {"type": "string",
                  "description": "pipeline 模式下的总任务"},
         "deliverable": {"type": "string",
                         "description": "交付形态，如「问题清单」「事实结论」"},
         "acceptance": {"type": "array", "items": {"type": "string"},
                        "description": "可机检验收条件：cites>=2 / answers>=1 / "
                                       "sources>=2 / contains:文本"},
         "background": {"type": "boolean",
                        "description": "后台运行：立即返回批次号，用 explore_status / "
                                       "explore_result 取结果"},
         "verify": {"type": "boolean",
                    "description": "追加反证者核对各代理的结论与证据"}},
        ["agents"]))
    out.append(_fn_tool(
        "explore_status",
        "查看后台子代理批次：状态（running / done / failed / cancelled）、"
        "已完成几个问题、部分结论、耗时。",
        {"batch": {"type": "string",
                   "description": "explore(background=true) 返回的批次号"}},
        ["batch"]))
    out.append(_fn_tool(
        "explore_result",
        "取后台子代理批次的完整汇总；还在跑时返回 running，先用 explore_status 看进度。",
        {"batch": {"type": "string", "description": "后台批次号"}},
        ["batch"]))
    out.append(_fn_tool(
        "explore_cancel",
        "取消还在跑的后台子代理批次。",
        {"batch": {"type": "string", "description": "后台批次号"}},
        ["batch"]))
    out.append(_fn_tool(
        "explore_followup",
        "在已探索的同一批子代理上继续追问（复用它们已读的上下文，比重新 explore 便宜）："
        "结论不够细、要确认某条分支或边界条件时用。batch 用 explore 结果里给出的批次号，"
        "子会话默认保留 600 秒。",
        {"batch": {"type": "string", "description": "explore 返回的批次号，如 ex_1a2b3c4d"},
         "questions": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "ask": {"type": "string", "description": "要追问的问题"},
                 "id": {"type": "string",
                        "description": "追问哪个子代理（q1 / q2 / critic）；省略 = 轮流分配"}},
             "required": ["ask"]}}},
        ["batch", "questions"]))
    out.append(_fn_tool(
        "acceptance",
        "可选的机检验收项（不是开工门槛）：每条写清「要验证什么」+「用哪个工具/命令检查」+「期望结果」。"
        "验收项根据任务自己推导，不要套模板。set 定义；run 跑安全只读检查；"
        "run_command 类检查在主循环执行后用 record 记录；"
        "标准过时用 revise 改 text/check/expect，或 drop=true 删除（必须写 reason）；"
        "status 查看；clear 清空。",
        {"action": {"type": "string",
                    "enum": ["set", "run", "record", "revise", "status", "clear"]},
         "criteria": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "id": {"type": "string"},
                 "text": {"type": "string", "description": "要验证什么"},
                 "check": {"type": "object",
                           "properties": {
                               "tool": {"type": "string"},
                               "args": {"type": "object"}},
                           "required": ["tool"]},
                 "expect": {"type": "object",
                            "description": "期望结果：contains / not_contains / regex；"
                                           "visual_check 可用 fps_min / fps_max / "
                                           "console_errors_max / draw_calls_max / sharpness_min / "
                                           "contrast_min / blank_score_max / ssim_min；"
                                           "asset_validate 可用 missing_max / unreadable_max / "
                                           "duplicates_max；mesh_inspect 可用 triangles_min / "
                                           "triangles_max / vertices_max / materials_max / "
                                           "textures_max / degenerate_max / missing_textures_max / "
                                           "bbox_x_max / bbox_y_max / bbox_z_max；命令类可为空"}},
             "required": ["text", "check"]}},
         "id": {"type": "string", "description": "record / revise 时的验收项 id"},
         "passed": {"type": "boolean", "description": "record 时该项是否通过"},
         "evidence": {"type": "string",
                      "description": "record 时的证据（命令输出 / 检查结果摘要）"},
         "reason": {"type": "string",
                    "description": "revise 时说明为什么原验收标准不再适用"},
         "drop": {"type": "boolean", "description": "revise 时直接删除这条过时的验收项"}},
        ["action"]))
    out.append(_fn_tool(
        "finish",
        "声明本轮任务已完成并交付（**显式收尾开关**）。只有确实做完、或确实要交给用户"
        "拍板时才调用；调用后本轮立刻结束。还有计划步骤没做完、或还要读 / 写 / 跑命令时"
        "**不要**调用它，直接继续干活。仅写正文、不调任何工具就停下会被系统判成「未完成」"
        "并要求你继续，所以收尾时请显式调用它。",
        {"summary": {"type": "string",
                     "description": "一句话说明交付了什么 / 结论"},
         "incomplete": {"type": "array", "items": {"type": "string"},
                        "description": "确实做不了、留给用户的后续事项（可选）"},
         "quality_waiver": {"type": "string",
                            "description": "可选：本轮用过 image_compare 但分数仍低于阈值时，"
                                           "写清为什么不再继续迭代（不写则可能被质量闸门拦下）"}},
        []))
    out.append(_fn_tool(
        "update_plan",
        "更新当前任务的执行计划（活文档，不是一次性清单）。需要动手的任务"
        "（改文件 / 跑命令 / 多步分析 / 读多个文件）**开工前必须先调用它**。"
        "大型任务用 milestones 拆成 2–6 个里程碑（每个里程碑要独立可验证、有可演示产物），"
        "每个里程碑 2–6 步；简单任务用 steps 给 1–8 步。"
        "每步写清「动词 + 目标 + 验证方式（verify）」；有依赖写 deps。"
        "执行中用 status 标 pending / doing / done / blocked。"
        "中途发现新事实、某步失败、或里程碑完成时，**不要另起一份新计划**，"
        "改用 plan_patch 增删改步骤并写 reason + log。"
        "纯聊天或单次只读不要调用。",
        {"goal": {"type": "string", "description": "这次任务的目标（一句话）"},
         "done_when": {"type": "array", "items": {"type": "string"},
                       "description": "整体完成判据：可机检 / 可演示的结果，2–6 条"},
         "milestones": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "id": {"type": "string", "description": "可选，如 M1；不填自动编号"},
                 "title": {"type": "string", "description": "里程碑要交付的能力（用户可见）"},
                 "done_when": {"type": "string", "description": "这个里程碑独立可验证的完成判据"},
                 "steps": {"type": "array", "items": {
                     "type": "object",
                     "properties": {
                         "id": {"type": "string", "description": "可选，如 M1S1"},
                         "text": {"type": "string", "description": "动词 + 目标（具体到文件 / 验证点）"},
                         "status": {"type": "string",
                                    "enum": ["pending", "doing", "done", "blocked"]},
                         "verify": {"type": "string", "description": "怎么验证这一步（命令 / 检查项）"},
                         "deps": {"type": "array", "items": {"type": "string"},
                                  "description": "依赖的步骤 id"},
                         "ref": {"type": "string",
                                 "description": "可选：证据引用（file:line / 验收项 id / 子代理批次），改计划时便于对照"},
                         "accept": {"type": "array", "items": {"type": "string"},
                                    "description": "可选：这一步对应的验收项 id（acceptance 工具里的 criteria.id）"},
                     },
                     "required": ["text"]}},
             },
             "required": ["title"]}},
         "steps": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "id": {"type": "string", "description": "可选；plan_patch 用 id 定位"},
                 "text": {"type": "string", "description": "这一步要做什么（具体到文件/验证点）"},
                 "status": {"type": "string", "enum": ["pending", "doing", "done", "blocked"]},
                 "milestone": {"type": "string", "description": "可选：归属哪个里程碑 id"},
                 "verify": {"type": "string", "description": "怎么验证这一步"},
                 "deps": {"type": "array", "items": {"type": "string"}},
                 "ref": {"type": "string", "description": "可选：证据引用（file:line / 验收项 id）"},
                 "accept": {"type": "array", "items": {"type": "string"},
                            "description": "可选：关联的验收项 id"},
             },
             "required": ["text"]}},
         "retro": {"type": "string",
                   "description": "可选：收尾复盘（实际结果 / 与计划的偏差 / 可复用经验），整份计划完成后写"},
         "log": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "kind": {"type": "string", "enum": ["decision", "discovery"]},
                 "text": {"type": "string"},
             },
             "required": ["kind", "text"]},
             "description": "可选：随计划一起记录决策 / 发现（最多各保留 20 条）"},
         "note": {"type": "string", "description": "可选备注"}},
        []))
    out.append(_fn_tool(
        "plan_patch",
        "在不重发整份计划的前提下调整现有计划（活文档）。用于：发现新事实 / 某步失败 / "
        "里程碑完成 / 用户改了要求。可改状态、拆步骤、加里程碑、删步骤，并把"
        "「为什么改」写进 reason、把发现写进 log。每次调用都会让计划版本 +1。",
        {"reason": {"type": "string", "description": "为什么改计划（写进决策日志）"},
         "goal": {"type": "string", "description": "可选：更新目标"},
         "done_when": {"type": "array", "items": {"type": "string"},
                       "description": "可选：更新整体完成判据"},
         "add_milestones": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "id": {"type": "string"},
                 "title": {"type": "string"},
                 "done_when": {"type": "string"},
                 "steps": {"type": "array", "items": {
                     "type": "object",
                     "properties": {
                         "text": {"type": "string"},
                         "status": {"type": "string",
                                    "enum": ["pending", "doing", "done", "blocked"]},
                         "verify": {"type": "string"},
                         "deps": {"type": "array", "items": {"type": "string"}},
                         "ref": {"type": "string"},
                         "accept": {"type": "array", "items": {"type": "string"}},
                     },
                     "required": ["text"]}},
             },
             "required": ["title"]}},
         "add_steps": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "milestone": {"type": "string", "description": "归属里程碑 id（默认当前里程碑）"},
                 "text": {"type": "string"},
                 "status": {"type": "string", "enum": ["pending", "doing", "done", "blocked"]},
                 "verify": {"type": "string"},
                 "deps": {"type": "array", "items": {"type": "string"}},
                 "ref": {"type": "string"},
                 "accept": {"type": "array", "items": {"type": "string"}},
             },
             "required": ["text"]}},
         "update_steps": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "id": {"type": "string", "description": "要改的步骤 id"},
                 "text": {"type": "string"},
                 "status": {"type": "string", "enum": ["pending", "doing", "done", "blocked"]},
                 "verify": {"type": "string"},
                 "milestone": {"type": "string"},
                 "deps": {"type": "array", "items": {"type": "string"}},
                 "ref": {"type": "string"},
                 "accept": {"type": "array", "items": {"type": "string"}},
             },
             "required": ["id"]}},
         "remove_steps": {"type": "array", "items": {"type": "string"},
                          "description": "要删除的步骤 id"},
         "retro": {"type": "string",
                   "description": "整份计划完成后写收尾复盘：实际结果 / 与计划的偏差 / 可复用经验"},
         "log": {"type": "array", "items": {
             "type": "object",
             "properties": {
                 "kind": {"type": "string", "enum": ["decision", "discovery"]},
                 "text": {"type": "string"},
             },
             "required": ["kind", "text"]},
             "description": "追加决策 / 发现（最多各保留 20 条）"}},
        []))
    out.append(_fn_tool(
        "run_workflow_script",
        "脚本化工作流：写一段 Python，用循环/条件/变量组合多次工具调用、多个 DAG 工作流"
        "与命名子代理。脚本里 wf.call(tool, **args)、wf.parallel([...])、wf.workflow(spec)"
        "（同步跑一个 DAG）、wf.workflow_text(spec)、wf.agent(name, task)、"
        "wf.agents([{agent, task}, ...])、wf.pipeline([names], task)、wf.log(...)；"
        "只有 print 的最终输出回传模型。"
        "适合「扫描 N 个文件 → 分组 → 每组派一个子代理 → 汇总」这类脚本天生擅长的编排。"
        "默认只读；完全放权或设置开关后可写/命令（脚本内不再逐次弹审批）；"
        "子代理的写权限跟随各自定义，每次写仍走审批。",
        {"code": {"type": "string", "description": "完整 Python 源码"},
         "timeout": {"type": "integer", "description": "超时秒数，默认 300，最大 600"}},
        ["code"]))
    out.append(_fn_tool(
        "run_workflow",
        "执行一段可恢复的多阶段工作流：节点用 needs 组成 DAG，产物用 {{id.output}} 引用。"
        "适合「探索 → 改多个文件 → 跑测试 → 校验 → 汇总」这类 3 步以上、有前后依赖的任务。"
        "每步落盘，中断后可 workflow_resume 续跑；写文件 / 命令仍按当前审批档位询问用户。"
        "节点类型：tool / command / script / explore / verify / final / loop / map / condition。"
        "loop 用 items + steps + max_iterations + until 逐项执行；map 用 items + node + parallel 扇出；"
        "condition 用 cond + then/else 选分支；when/until 支持 == != > < >= <= contains && || !；"
        "step 内可用 {{item}} / {{index}} 引用当前迭代。",
        {"workflow": {"type": "object", "description": "工作流定义", "properties": {
            "name": {"type": "string"},
            "fail_fast": {"type": "boolean", "description": "失败就停（默认 true）"},
            "budget": {"type": "object", "properties": {
                "tokens": {"type": "integer", "description": "token 上限，0 = 不限"},
                "seconds": {"type": "integer", "description": "秒数上限，0 = 不限"}}},
            "steps": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string", "description": "节点名，后续用 {{id.output}} 引用"},
                "type": {"type": "string",
                         "enum": ["tool", "command", "script", "explore", "verify", "final",
                                  "loop", "map", "condition"]},
                "needs": {"type": "array", "items": {"type": "string"},
                          "description": "依赖节点；省略 = 串到上一个，[] = 并行起点"},
                "items": {"description": "type=loop/map 时的数组，或 {{id.output}} 引用"},
                "steps": {"type": "array", "items": {"type": "object"},
                          "description": "type=loop 的子步骤"},
                "node": {"type": "object", "description": "type=map 的扇出模板"},
                "parallel": {"type": "integer", "description": "type=map 的并发数（1–16）"},
                "collect": {"type": "string", "enum": ["list", "join"]},
                "cond": {"type": "string", "description": "type=condition 的条件"},
                "then": {"type": "array", "items": {"type": "object"}},
                "else": {"type": "array", "items": {"type": "object"}},
                "max_iterations": {"type": "integer",
                                   "description": "type=loop 的迭代上限（≤100）"},
                "until": {"type": "string", "description": "type=loop 的提前结束条件"},
                "on_error": {"type": "string", "enum": ["stop", "continue"]},
                "when": {"type": "string",
                         "description": "条件，如 {{test.exit_code}} == 0"},
                "tool": {"type": "string", "description": "type=tool 时的工具名"},
                "args": {"type": "object"},
                "argv": {"type": "array", "items": {"type": "string"},
                         "description": "type=command 时的参数数组"},
                "code": {"type": "string", "description": "type=script 时的 Python 源码"},
                "goal": {"type": "string", "description": "type=explore 时的探索目标"},
                "questions": {"type": "array", "items": {"type": "string"}},
                "checks": {"type": "array", "items": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "沙箱内相对路径"},
                    "exists": {"type": "boolean"},
                    "contains": {"type": "string"},
                    "not_contains": {"type": "string"},
                    "regex": {"type": "string"}}}},
                "template": {"type": "string", "description": "type=final 的汇总模板"},
                "retries": {"type": "integer"},
                "timeout": {"type": "integer"},
                "if_exists": {"type": "string", "enum": ["", "skip", "overwrite"]},
                "always": {"type": "boolean", "description": "依赖失败也执行"}}}}}}},
        ["workflow"]))
    out.append(_fn_tool(
        "workflow_status", "查看工作流每个节点的状态与输出（每个节点最多 800 字）",
        {"id": {"type": "string", "description": "工作流 id，形如 wf_1a2b3c4d"}},
        ["id"]))
    out.append(_fn_tool(
        "workflow_resume", "从断点继续执行工作流：已完成的节点不会重跑",
        {"id": {"type": "string", "description": "工作流 id"}}, ["id"]))
    out.append(_fn_tool(
        "workflow_cancel", "取消工作流（进行中的节点跑完后停止，不再启动新节点）",
        {"id": {"type": "string", "description": "工作流 id"}}, ["id"]))


    try:
        from app import capabilities
        caps = capabilities.chat_items()
    except Exception:
        caps = []
    for cap in caps:
        if cap_domains is not None:
            dom = str(cap.id or "").split(".")[0]
            if dom not in cap_domains:
                continue
        props, required = {}, []
        for key, spec in (cap.schema or {}).items():
            props[key] = {"type": _json_type(spec)}
            if not str(spec or "").strip().endswith("?"):
                required.append(key)
        out.append(_fn_tool(cap.id.replace(".", "_"), cap.description,
                            props, required or None))
    # 插件工具（data/plugins/*.py 用 @agent_tool 注册）；插件默认关闭
    with _PLUGIN_TOOLS_LOCK:
        _plug = list(_PLUGIN_TOOLS.items())
    for _nm, _meta in _plug:
        _sch = _meta.get("schema") or {}
        _desc = _meta.get("description") or _nm
        if isinstance(_sch, dict) and _sch.get("type") == "object":
            out.append({"type": "function",
                        "function": {"name": _nm, "description": _desc,
                                     "parameters": _sch}})
            continue
        _props, _req = {}, []
        for _k, _spec in (_sch or {}).items():
            _props[_k] = {"type": _json_type(_spec)}
            if not str(_spec or "").strip().endswith("?"):
                _req.append(_k)
        out.append(_fn_tool(_nm, _desc, _props, _req or None))
    # 外部 MCP 工具：默认只挂两个元工具，模型显式解锁后下一轮才挂具体 server 的工具
    try:
        from app import mcp
        out += mcp.tool_schemas()
    except Exception:                     # noqa: BLE001  外部服务故障不影响内置工具
        pass
    # 工具分层：低频工具组默认不下发（省掉每轮重发的 schema），
    # 命中意图域（allow_groups）或模型用 tool_search 解锁后才挂上。
    if allowed_tools is not None:
        # 子代理 / 评测的角色白名单：按旧工具名授权，meta 只暴露被授权的 action。
        _allow = set()
        for _n in allowed_tools:
            _n = str(_n or "").strip()
            if _n in tool_meta.META:
                _allow.update(legacy for legacy, _r, _m
                              in tool_meta.META[_n]["actions"].values())
            elif _n:
                _allow.add(_n)
        out = [t for t in out
               if str((t.get("function") or {}).get("name") or "") in _allow]
    else:
        hide = hidden_tools(allow_groups, unlocked)
        if hide:
            before = len(out)
            out = [t for t in out
                   if str((t.get("function") or {}).get("name") or "") not in hide]
            if len(out) < before:
                out.append(_tool_search_schema())
    if plan_mode:
        # /plan 规划轮：只把只读工具 + 计划工具交给模型，写 / 命令 / Git 提交
        # 连 schema 都不下发，避免它先写一版再被运行时拒绝。
        out = [t for t in out
               if ((t.get("function") or {}).get("name") in PLAN_MODE_TOOLS)]
    # 工具元层：把 lsp_* / git_* / plan_* / 文件操作等收敛为少量 action 工具。
    # 被隐藏或 plan_mode 过滤掉的 action 不会出现在 enum 里；旧工具名仍可调用。
    _names = [str((t.get("function") or {}).get("name") or "") for t in out]
    _metas, _used = tool_meta.build_schemas(_names)
    if _used:
        out = _metas + [t for t in out
                        if str((t.get("function") or {}).get("name") or "") not in _used]
    return out


def agent_tool(name, description, schema=None, permission="write"):
    """插件注册 Agent 工具的装饰器（`data/plugins/*.py`，需 plugins_enabled）。

    handler 签名：fn(root, args) -> str | (str, undo_ops)
    返回值以 ❌ 开头表示失败，与内置工具一致。
    默认 permission=write（走审批）；声明 permission="read" 才免审批。
    """
    tname = str(name or "").strip()
    if not tname:
        raise ValueError("agent_tool 需要工具名")
    perm = "read" if str(permission or "").strip().lower() == "read" else "write"

    def deco(fn):
        def _call(root, args, allow_outside=False):
            res = fn(root, args if isinstance(args, dict) else {})
            if isinstance(res, tuple):
                return res[0], (res[1] if len(res) > 1 else None)
            return res, None

        with _PLUGIN_TOOLS_LOCK:
            _PLUGIN_TOOLS[tname] = {"description": str(description or ""),
                                    "schema": dict(schema or {}),
                                    "permission": perm, "handler": fn}
            _HANDLERS[tname] = _call
        return fn

    return deco
