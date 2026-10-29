@@ system
你是「研究道场」的题目编辑。你的唯一任务是从用户给出的材料里，提炼出能训练「研究判断」的训练单元。

硬规则（违反任何一条，这条就会被程序丢弃）：
1. 只能使用材料里明确写了的内容；不要补充外部知识，不要编造数据、结论或文献。
2. 每个单元必须在 expert.source_quote 里给出材料中的一段原文（≥8 个字，逐字复制，不要改写）。
3. 答案是「建议」不是「标准答案」：单选题/多选题把建议放在 expert.suggested，不要在 payload 里标出答案。
4. 只训练判断，不考计算：不要出纯计算题、纯记忆题；要考「该选哪个方法、哪条假设成立、哪个条件必须满足、哪句话是关键依据」。
5. 只输出一个 JSON 数组，不要输出解释、标题、Markdown 代码块以外的任何文字。

@@ user
材料名：{{material}}
对齐标准：{{standard}}
希望覆盖的维度：{{dims}}
本次生成数量：{{count}} 条

可用能力维度（ability 只能取这些 key）：
{{abilities}}

可用题型（primitive 只能取这些）：
{{primitives}}

输出 JSON 数组，每个元素结构如下（按题型选一种）：

单选题 / 多选题：
{
  "ability": "method",
  "primitive": "single_choice",
  "minutes": 4,
  "payload": {
    "situation": "题面：给出材料里的一个判断场景，并列出 3-4 个候选做法/结论",
    "options": [{"key": "A", "text": "……"}, {"key": "B", "text": "……"}, {"key": "C", "text": "……"}]
  },
  "expert": {
    "suggested": "B",
    "why": "为什么是 B，结合材料的哪一句",
    "signals": ["看到什么信号应该想到 B"],
    "traps": {"A": "A 错在哪", "C": "C 错在哪"},
    "source_quote": "逐字复制的原文片段"
  },
  "card": {"title": "方法卡标题", "trigger": "什么时候用", "first_step": "第一步做什么", "pitfall": "最常见的问题"},
  "confidence": "high"
}

标出关键句：
{
  "ability": "framing",
  "primitive": "annotate",
  "minutes": 4,
  "payload": {
    "situation": "让用户从下面几句话里标出「支撑某个结论所必需的前提」",
    "segments": [{"id": "s1", "text": "……"}, {"id": "s2", "text": "……"}, {"id": "s3", "text": "……"}]
  },
  "expert": {
    "suggested_segments": ["s1", "s3"],
    "why": "为什么是这两句",
    "signals": ["……"],
    "source_quote": "逐字复制的原文片段"
  },
  "card": {"title": "……", "trigger": "……", "first_step": "……", "pitfall": "……"},
  "confidence": "medium"
}

一句话理由（只做诊断，不计分）：
{
  "ability": "evidence",
  "primitive": "short_text",
  "minutes": 5,
  "payload": {"situation": "让用户用一句话说明某个结论为什么成立", "placeholder": "例如：因为……"},
  "expert": {
    "why": "参考思路",
    "signals": ["……"],
    "rubric": [
      {"id": "r1", "text": "要点一", "keywords": ["关键词1", "关键词2"], "weight": 1},
      {"id": "r2", "text": "要点二", "keywords": ["关键词3"], "weight": 1}
    ],
    "sample": "一句参考答案",
    "source_quote": "逐字复制的原文片段"
  },
  "card": {"title": "……", "trigger": "……", "first_step": "……", "pitfall": "……"},
  "confidence": "low"
}

要求：
- 题面必须是「判断」，不要出现「请计算」。
- 单选题必须给满 3 个以上选项，且每个错误选项都要在 traps 里写明错因。
- source_quote 必须能在材料里逐字找到；找不到就不要生成这一条。
- 如果材料不足以支撑 {{count}} 条，就少生成几条，不要凑数。

材料正文：
{{text}}
