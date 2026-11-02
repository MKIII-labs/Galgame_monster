# 评测协议（可复现）

目标：同一份代码、同一个模型、同一组任务，任何人重跑都能得到相近结果；失败题也保留。

## 两个工具

| 工具 | 作用 | 是否调用模型 |
|---|---|---|
| `python scripts/bench.py` | 文件 / 命令 / 视觉类任务（`resources/eval/bench_tasks.json`） | 是 |
| `python scripts/eval_research_run.py` | 检索 / 复刻类任务（`resources/eval/research_tasks.json`） | 是 |
| `python scripts/eval_research.py` | 对**已有会话**做机检（不跑模型） | 否 |

## 固定变量

每次 run 都会写 `env.json`，包含：

- 模型名、温度（评测固定 `0`）、重复次数、仓库 commit、是否有未提交改动
- Python / 平台 / 应用版本 / 运行时间
- 单题 `max_turns` 与 `timeout`（默认 12 轮 / 300 秒；研究任务 6 轮 / 240 秒）

不包含：API Key、模型服务地址、浏览器 profile。

## 跑法

```bash
# 1. 只校验任务集，不花钱
python scripts/bench.py dry-run
python scripts/eval_research_run.py --list

# 2. 先跑一条烟雾测试
python scripts/bench.py run --only bug_fix --repeat 1 --out eval/runs/smoke

# 3. 正式基线：每条重复 3 次
python scripts/bench.py run --repeat 3 --out eval/runs/2026-11-01/<model>

# 4. 消融：基线 + 单个消融分别跑（回答“哪层在起作用”）
python scripts/bench.py run --repeat 3 --ablate no_subagent,no_code_mode \
  --out eval/runs/2026-11-01/<model>-ablate

# 5. 研究评测
python scripts/eval_research_run.py --repeat 3 --out eval/runs/2026-11-01/research
```

## 结果目录

```
eval/runs/<日期>/<模型>/
  env.json          环境快照
  summary.md        人看：按任务通过率 + 每次运行明细
  summary.json      机读汇总（by_task / by_ablation）
  <task>.json       第一次运行
  <task>.2.json     第二次运行（重复不覆盖）
  <task>.3.json
```

## 判定口径

- `bench`：任务自己的 `expect` 断言（文件内容 / 命令退出码 / 视觉指标 / 工具轨迹）全部通过才算过。
- `research`：`must_search` 次数、写文件后是否补搜、是否有真实来源、`key_facts` 覆盖率 ≥ 50% 才算过。
- 研究评测是**保守机检**，只用于发现明显跑偏，不等于文风或结论质量评分。

## 单代理 vs 团队

```bash
# 团队模式：任务提示里要求优先用 delegate 派活
python scripts/bench.py run --only tool_pipeline --repeat 1 --team \
  --out eval/runs/team-smoke

# 对照组：同样提示，但移除 delegate 工具
python scripts/bench.py run --only tool_pipeline --repeat 1 --team \
  --ablate no_delegate --out eval/runs/team-smoke
```

- `--team`：在任务 prompt 末尾追加「独立子任务优先 delegate」。
- `--ablate no_delegate`：基线 + 移除 `delegate` 的消融，两者会在同一次 run 里分两组。
- 注意：当前 6 题偏小，团队往往只增加轮次与耗时；它的价值在跨模块、需要反证或可拆分的任务上。

首轮结果（2026-10-31，`tool_pipeline`，`deepseek-flash`）：

| 组 | 轮次 | 工具 | 耗时 | 结果 |
|---|---:|---:|---:|---|
| 团队（delegate 可用） | 9 | 10 | 29.1s | 通过 |
| 无 delegate | 5 | 6 | 11.1s | 通过 |

团队组实际派出 `reviewer` 复核 `summary.json`，子代理 6.8s / 7196 token 返回逐项核对结论。结论：**小任务上没有准确率收益，成本约 2.6 倍**；要证明团队价值，需要更大的任务集。

## 发布时一起给出

1. `summary.md` + `summary.json` + `env.json` + 原始 trace（`<task>*.json` 里的 `tools`）。
2. 重复 3 次的通过率，不要只报最好一次。
3. 失败题与失败原因。
4. 明确区分「模型分数」与「harness 分数」：同模型再跑一次裸循环基线，差值才是 harness 的贡献。

## 已知局限

- `temperature=0` 也不保证完全确定，必须看重复区间。
- `bench` 走内部循环，不经过 HTTP / 审批 / 记忆 / 历史压缩，和产品实际链路有差异。
- 任务集目前只有 6 + 12 条，样本小，不适合对外宣称绝对强弱。
