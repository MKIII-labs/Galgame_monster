---
name: 编码四律
description: 当开始写代码、改代码、修 bug、重构或做多步任务时使用；用四条行为守则减少假设、过度设计、范围蔓延和无效验证
keywords: 编码, 写代码, 改代码, 修 bug, 重构, 假设, 简洁, 外科手术式修改, 验证, 过度设计, 范围蔓延, CLAUDE.md, Karpathy
enabled: true
---

# 编码四律

来源：Andrej Karpathy 的 CLAUDE.md，GitHub 上广泛传播的四条编码行为守则。原文保留在下方，前面补了在本项目里的执行顺序。

## 何时用

- 接到编码 / 改代码 / 修 bug / 重构任务
- 准备做多步改动、跨文件改动或不可逆操作
- 想凭印象直接写代码，或需求边界还不清楚时
- 交付前需要判断「是不是真的做完、验证过」

## 执行顺序

1. **先对齐**：按第 1 条说清目标、假设和取舍；不确定就问，不要沉默地选一种解释。
2. **再砍范围**：按第 2 条只做被要求的事；不预埋抽象、配置和「以后可能用得上」的代码。
3. **动手时收窄 diff**：按第 3 条只碰必须碰的行；不顺手重构、不清理无关死代码。
4. **收尾给证据**：按第 4 条把任务改写成可验证目标；没跑过验证不要说完成。

## 规则原文

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

## 交付检查

- 我明确说了假设吗？不确定的地方问了吗？
- 有没有做用户没要求的功能、抽象或配置？
- diff 里每一行都能追溯到用户请求吗？
- 验收点是什么？我实际跑了哪条命令、看到什么结果？
- 哪些部分通过、哪些没通过、哪些没有覆盖？
