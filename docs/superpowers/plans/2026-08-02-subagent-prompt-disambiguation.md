# 子 Agent 提示词歧义修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除子 Agent 提示词中的两类歧义：(1) 格式错误提示中"调用工具继续工作"让子 Agent 误以为工具调用也是一种合法输出格式；(2) 游标输出指令 `processed_up_to=N` 缺少 `@end` 前缀，子 Agent 不知道这是结束信号。

**Architecture:** 纯文本修改，不涉及逻辑变更。格式错误提示去掉第1条；游标指令统一改为"以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`"。EXIT 处理逻辑（agent_loop.py L744-746）会剥除 `@end` 前缀保留后续内容，因此 `_parse_processed_up_to` 收到的是不含 `@end` 的 `processed_up_to=N`，现有正则直接匹配，无需改动解析逻辑。

**Tech Stack:** Python（agent_loop.py / compat.py / runner.py）、Markdown（config/agents/*.md）

---

## 背景

### 问题 1：格式错误提示歧义

`agent/generic/agent_loop.py` 的 `_FORMAT_ERROR_PROMPT`（L16-22）：

```python
_FORMAT_ERROR_PROMPT = (
    "[对话格式错误] 你的输出必须遵循以下格式之一：\n"
    "1. 调用工具继续工作（正常 tool_calls）\n"
    f"2. 询问主 Agent：content 以 `{_AT_NIU_PREFIX} ` 开头，如 `{_AT_NIU_PREFIX} 我应该选择哪个选项？`\n"
    "3. 结束会话：content 以 `@end ` 开头，如 `@end 任务已完成，结果：...`\n"
    "禁止输出不带 @ 前缀的纯 content。请重新输出。"
)
```

**问题**：第1条"调用工具继续工作"让子 Agent 产生歧义——它认为"我已经用了工具调用"就是合法输出，不认为自己的回复有格式错误。然后它可能先输出一轮工具调用，再来找 @ 前缀。

**触发场景**：子 Agent 返回纯 content（无 tool_calls、无 @niu-agent、无 @end）时，系统注入此提示。此时 `_intercept_at_prefix_content` 在 L113-114 因 `response.tool_calls` 为空已跳过 NO_INTERCEPTION 检查——格式错误只在纯 content 时触发，工具调用永远不会触发。因此第1条"调用工具继续工作"是冗余且歧义的。

**修复**：去掉第1条，只保留 2 和 3。子 Agent 只需在两种 content 格式中二选一。

### 问题 2：游标输出指令缺少 @end 前缀

三个子 Agent（entity-extractor / dream-evolver / journal-agent）的提示词中，游标输出指令是：

```
处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。
```

**问题**：告诉子 Agent 最后一行输出 `processed_up_to=N`，但没告诉它要 `@end`。子 Agent 输出 `processed_up_to=15` 后不知道要结束会话——它是纯 content，会触发格式错误提示，然后子 Agent 才意识到要加 @end。

**修复**：统一改为"以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`"。

**为什么 @end 在开头而不是末尾**：EXIT 处理逻辑（agent_loop.py L744-746）执行 `exit_content = stripped_content[at_end_idx + 4:].lstrip()`，保留 `@end` **之后**的所有内容，丢弃 `@end` **之前**的内容。如果子 Agent 输出 `报告内容\n@end processed_up_to=5`，报告内容会被丢弃。因此 `@end` 必须在开头：`@end 报告内容\nprocessed_up_to=5` → `exit_content` = `报告内容\nprocessed_up_to=5`，报告保留，`_parse_processed_up_to` 也能提取 N=5。

这与 `_FORMAT_ERROR_PROMPT` 中的示例 `@end 任务已完成，结果：...` 一致——@end 在开头，后接内容。

### 解析逻辑兼容性

EXIT 处理逻辑（agent_loop.py L741-754）的完整流程：

1. `_intercept_at_prefix_content` 检测到 `@end` → 返回 `(EXIT, None)`
2. agent_runner_loop L744-746：`exit_content = stripped_content[at_end_idx + 4:].lstrip()` — 剥除 `@end` 前缀，保留后续内容
3. L752：`yield StreamEvent("reply", exit_content)` — 推送剥除前缀后的内容
4. `_run_agent_loop`（subagent.py L288-289）收集 `result += chunk.content` — result 不含 `@end`
5. `_parse_processed_up_to(result)` 用正则 `r'processed_up_to\s*[=:\s]\s*(\d+)'` 匹配 — 直接匹配 `processed_up_to=N`

因此 `_parse_processed_up_to` 收到的是不含 `@end` 的 `processed_up_to=N`，现有正则直接匹配。**无需改动解析逻辑。**

### 修改位置清单

**格式错误提示（1 处）**：
- `agent/generic/agent_loop.py` L16-22

**游标指令（11 处 + 1 处 docstring）**：

| # | 文件 | 行号 | 子 Agent | 上下文 |
|---|------|------|----------|--------|
| 1 | `agent/runner.py` | L1228 | entity-extractor | force prompt |
| 2 | `agent/runner.py` | L1269 | dream-evolver | force prompt |
| 3 | `niu_api/compat.py` | L1051 | journal-agent | docstring |
| 4 | `niu_api/compat.py` | L1055 | journal-agent | `_build_journal_task()` |
| 5 | `niu_api/compat.py` | L2477 | entity-extractor | tidy prompt |
| 6 | `niu_api/compat.py` | L2560 | dream-evolver | tidy prompt |
| 7 | `niu_api/compat.py` | L3345 | entity-extractor | force prompt |
| 8 | `niu_api/compat.py` | L3428 | dream-evolver | force prompt |
| 9 | `config/agents/dream-evolver.md` | L482 | dream-evolver | 游标机制段 |
| 10 | `config/agents/entity-extractor.md` | L29 | entity-extractor | 输入规范段 |
| 11 | `config/agents/entity-extractor.md` | L82 | entity-extractor | 游标机制段 |
| 12 | `config/agents/journal-agent.md` | L28 | journal-agent | 输入格式段 |

**验证脚本（1 处）**：
- `tests/verify_llm_at_prefix.py` L20-22 — 硬编码了旧 3 选项格式，需同步更新为 2 选项

**不修改的位置**：
- `niu_api/compat.py` L619, L635, L668-694 — 这些是 `_parse_processed_up_to` 解析器的 docstring，描述解析器接受的格式（不含 @end），不是给子 Agent 的输出指令
- `config/agents/context-manager.md` — context-manager 使用 `keep=/update=/cursor=` 格式且 `bypass_at_prefix=True`，不使用 processed_up_to 指令
- `agent/generic/agent_loop.py` L249-256 `_check_main_agent_content_reply_to_suspended` 的 error_prompt — 这是主 Agent 误回复挂起子 Agent 的错误提示，与子 Agent 格式错误提示是完全独立的上下文
- `config/agents/journal-agent.md` L29 "此时无需输出 `processed_up_to=`" — 报告生成场景的说明，不修改

---

## File Structure

- Modify: `agent/generic/agent_loop.py` — `_FORMAT_ERROR_PROMPT` 去掉第1条
- Modify: `agent/runner.py` — 2 处游标指令
- Modify: `niu_api/compat.py` — 5 处游标指令 + 1 处 docstring
- Modify: `config/agents/dream-evolver.md` — 1 处游标指令
- Modify: `config/agents/entity-extractor.md` — 2 处游标指令
- Modify: `config/agents/journal-agent.md` — 1 处游标指令
- Modify: `tests/verify_llm_at_prefix.py` — 同步格式错误提示为 2 选项

---

## Task 1: 修复格式错误提示

**Files:**
- Modify: `agent/generic/agent_loop.py:16-22`

- [ ] **Step 1: 修改 `_FORMAT_ERROR_PROMPT`**

将 L16-22 的 `_FORMAT_ERROR_PROMPT` 替换为：

```python
_FORMAT_ERROR_PROMPT = (
    "[对话格式错误] 你的输出必须遵循以下格式之一：\n"
    f"1. 询问主 Agent：content 以 `{_AT_NIU_PREFIX} ` 开头，如 `{_AT_NIU_PREFIX} 我应该选择哪个选项？`\n"
    "2. 结束会话：content 以 `@end ` 开头，如 `@end 任务已完成，结果：...`\n"
    "禁止输出不带 @ 前缀的纯 content。请重新输出。"
)
```

变化：删除 `"1. 调用工具继续工作（正常 tool_calls）\n"` 行，原来的 2→1、3→2。

- [ ] **Step 2: 验证语法正确**

Run: `python/bin/python -c "from agent.generic.agent_loop import _FORMAT_ERROR_PROMPT; print(_FORMAT_ERROR_PROMPT)"`
Expected: 输出 2 条选项（1. 询问主 Agent / 2. 结束会话），无第1条"调用工具"

- [ ] **Step 3: Commit**

```bash
git add agent/generic/agent_loop.py
git commit -m "fix: remove ambiguous 'tool_calls' option from subagent format error prompt"
```

---

## Task 2: 修复 runner.py 游标指令（2 处）

**Files:**
- Modify: `agent/runner.py:1228`
- Modify: `agent/runner.py:1269`

两处修改的核心变化：将"在最终回复的最后一行输出 `processed_up_to=N`"改为"以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`"。

- [ ] **Step 1: 修改 L1228 entity-extractor force prompt**

原文本（L1228，entity_force_prompt 的最后一行）：

```
处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

替换为：

```
处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

- [ ] **Step 2: 修改 L1269 dream-evolver force prompt**

原文本（L1269，dream_force_prompt 的最后一行）：

```
消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

替换为：

```
消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

- [ ] **Step 3: Commit**

```bash
git add agent/runner.py
git commit -m "fix: use @end prefix in cursor instruction in runner.py subagent prompts"
```

---

## Task 3: 修复 compat.py 游标指令（5 处 + 1 docstring）

**Files:**
- Modify: `niu_api/compat.py:1051`
- Modify: `niu_api/compat.py:1055`
- Modify: `niu_api/compat.py:2477`
- Modify: `niu_api/compat.py:2560`
- Modify: `niu_api/compat.py:3345`
- Modify: `niu_api/compat.py:3428`

核心变化同 Task 2：将"在最终回复的最后一行输出 `processed_up_to=N`"改为"以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`"。

- [ ] **Step 1: 修改 L1051 docstring**

原文本：

```python
        纯指令 task prompt 字符串（含 processed_up_to=N 说明，程序据此推进游标）
```

替换为：

```python
        纯指令 task prompt 字符串（含 @end processed_up_to=N 说明，程序据此推进游标）
```

- [ ] **Step 2: 修改 L1055 journal-agent task prompt**

原文本（`_build_journal_task()` 返回值的最后一行）：

```
处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

替换为：

```
处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

- [ ] **Step 3: 修改 L2477 entity-extractor tidy prompt**

原文本（entity_task_prompt 的最后一行）：

```
处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

替换为：

```
处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

- [ ] **Step 4: 修改 L2560 dream-evolver tidy prompt**

原文本（dream_task_prompt 的最后一行）：

```
消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

替换为：

```
消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

- [ ] **Step 5: 修改 L3345 entity-extractor force prompt**

原文本（entity_force_prompt 的最后一行）：

```
处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

替换为：

```
处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

- [ ] **Step 6: 修改 L3428 dream-evolver force prompt**

原文本（dream_force_prompt 的最后一行）：

```
消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

替换为：

```
消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

- [ ] **Step 7: 验证无残留**

Run: `grep -n "在最终回复的最后一行输出.*processed_up_to" niu_api/compat.py`
Expected: 无输出（所有指令性文本已改为新措辞）

注意：compat.py L619, L635, L668-694 的 docstring 中含有 `processed_up_to=N` 但属于解析器格式描述，不是指令性文本，不应修改。上述 grep 只匹配指令性文本（"在最终回复的最后一行输出"），不会误报。

- [ ] **Step 8: Commit**

```bash
git add niu_api/compat.py
git commit -m "fix: use @end prefix in cursor instruction in compat.py subagent prompts"
```

---

## Task 4: 修复 config/agents 游标指令（4 处）

**Files:**
- Modify: `config/agents/dream-evolver.md:482`
- Modify: `config/agents/entity-extractor.md:29`
- Modify: `config/agents/entity-extractor.md:82`
- Modify: `config/agents/journal-agent.md:28`

- [ ] **Step 1: 修改 dream-evolver.md L482**

原文本：

```
2. 处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出，程序会回退到区间末尾作为游标（兜底）
```

替换为：

```
2. 处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出，程序会回退到区间末尾作为游标（兜底）
```

- [ ] **Step 2: 修改 entity-extractor.md L29**

原文本：

```
- **处理完成后，在最终回复的最后一行输出 `processed_up_to=N`**（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出，程序会回退到区间末尾作为游标（兜底）
```

替换为：

```
- **处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`**（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出，程序会回退到区间末尾作为游标（兜底）
```

- [ ] **Step 3: 修改 entity-extractor.md L82**

原文本：

```
- 你必须输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标
```

替换为：

```
- 你必须以 `@end` 开头输出最终回复，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标
```

- [ ] **Step 4: 修改 journal-agent.md L28**

原文本（L28 中"日志记录"场景的最后一句）：

```
处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出，程序会回退到区间末尾作为游标（兜底）。
```

替换为：

```
处理完成后，以 `@end` 开头输出最终回复，后接报告内容，最后一行包含 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。如果未输出，程序会回退到区间末尾作为游标（兜底）。
```

注意：journal-agent.md L29 的"此时无需输出 `processed_up_to=`"不修改——报告生成场景本来就不输出游标。

- [ ] **Step 5: 验证无残留**

Run: `grep -rn "在最终回复的最后一行输出.*processed_up_to" config/agents/`
Expected: 无输出

- [ ] **Step 6: Commit**

```bash
git add config/agents/dream-evolver.md config/agents/entity-extractor.md config/agents/journal-agent.md
git commit -m "fix: use @end prefix in cursor instruction in subagent agent definition files"
```

---

## Task 5: 同步验证脚本

**Files:**
- Modify: `tests/verify_llm_at_prefix.py:20-22`

`tests/verify_llm_at_prefix.py` 的 SYSTEM_PROMPT 硬编码了旧 3 选项格式，需同步为 2 选项。

- [ ] **Step 1: 读取当前内容**

Run: `head -30 tests/verify_llm_at_prefix.py`

确认 L18-26 的 SYSTEM_PROMPT 当前是 3 选项格式（含"1. 调用工具继续工作"）。

- [ ] **Step 2: 修改为 2 选项**

将 SYSTEM_PROMPT 中硬编码的 3 选项改为 2 选项，删除"1. 调用工具继续工作"行，原 2→1、3→2。

原内容（L18-26）：

```python
SYSTEM_PROMPT = """你是一个异步子 Agent。每轮输出必须遵循以下格式：

1. 调用工具继续工作：正常 tool_calls
2. 询问主 Agent（不退出，等主 Agent 回答后继续）：content 必须以 `@niu-agent ` 开头，如 `@niu-agent 我应该选择哪个选项？`
3. 结束会话（任务完成或无法继续）：content 必须以 `@end ` 开头，如 `@end 任务已完成，结果：...`

**重要**：禁止输出不带 @ 前缀的纯 content（会被程序拒绝并要求重新输出）。
遇到需要用户决策的问题时，必须用 `@niu-agent` 询问，禁止直接把问题写在 content 里。
"""
```

替换为：

```python
SYSTEM_PROMPT = """你是一个异步子 Agent。每轮输出必须遵循以下格式：

1. 询问主 Agent（不退出，等主 Agent 回答后继续）：content 必须以 `@niu-agent ` 开头，如 `@niu-agent 我应该选择哪个选项？`
2. 结束会话（任务完成或无法继续）：content 必须以 `@end ` 开头，如 `@end 任务已完成，结果：...`

**重要**：禁止输出不带 @ 前缀的纯 content（会被程序拒绝并要求重新输出）。
遇到需要用户决策的问题时，必须用 `@niu-agent` 询问，禁止直接把问题写在 content 里。
"""
```

变化：删除"1. 调用工具继续工作：正常 tool_calls"行，原 2→1、3→2。其余文本（介绍语、重要提示）不变。

- [ ] **Step 3: Commit**

```bash
git add tests/verify_llm_at_prefix.py
git commit -m "fix: sync verify_llm_at_prefix.py with updated format error prompt"
```

---

## Task 6: 验证

- [ ] **Step 1: 验证解析逻辑兼容**

Run:
```bash
python/bin/python -c "
import re
# 模拟 EXIT 处理后 _parse_processed_up_to 收到的内容（@end 已被剥除）
exit_content = '[梦境进化报告]\n处理范围：共 5 条消息\nprocessed_up_to=5'
match = re.search(r'processed_up_to\s*[=:\s]\s*(\d+)', exit_content, re.IGNORECASE)
print(f'match: {match.group(1) if match else None}')
assert match.group(1) == '5', 'regex should match processed_up_to=5 in exit_content'
print('PASS')
"
```
Expected: `match: 5` + `PASS`

- [ ] **Step 2: 验证全局无残留旧格式**

Run: `grep -rn "在最终回复的最后一行输出.*processed_up_to" agent/ niu_api/ config/agents/`
Expected: 无输出

- [ ] **Step 2b: 验证新措辞正向覆盖**

Run: `grep -rn "最后一行包含.*processed_up_to" agent/ niu_api/ config/agents/ | wc -l`
Expected: `11`（11 处游标指令替换，entity-extractor.md L82 用简化措辞"最后一行包含"也在内）

如果计数不足 11，说明某处被误删而非替换，需检查对应文件。

- [ ] **Step 3: 运行现有测试**

Run: `python/bin/python -m pytest tests/test_at_prefix_interception.py tests/test_dynamic_injection.py tests/test_dream_split.py tests/test_protect_range.py tests/test_sep_cleanup.py -v`
Expected: 全部 PASS（可能有 pre-existing skip）

- [ ] **Step 4: 验证格式错误提示**

Run:
```bash
python/bin/python -c "
from agent.generic.agent_loop import _FORMAT_ERROR_PROMPT
assert '调用工具继续工作' not in _FORMAT_ERROR_PROMPT, 'should not contain tool_calls option'
assert '1. 询问主 Agent' in _FORMAT_ERROR_PROMPT, 'option 1 should be ask main agent'
assert '2. 结束会话' in _FORMAT_ERROR_PROMPT, 'option 2 should be end session'
print('PASS')
print(_FORMAT_ERROR_PROMPT)
"
```
Expected: `PASS` + 2 条选项的提示文本
