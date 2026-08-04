# 异步子 Agent 完成通知内容截断修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复异步子 Agent 完成通知把所有轮次 reply 累加后截断到 2000 字符，导致主 Agent 收到的是工作过程而非最终结果。

**Architecture:** `_run_agent_loop` 中 `result` 累加了所有轮次的 reply 文本。`_run_subagent_async` 的完成通知用 `result[:2000]` 截断后推给主 Agent。修复方案：`_run_agent_loop` 新增 `last_reply` 只记录最后一次 reply，完成通知用 `last_reply` 替代 `result[:2000]`，同时保留合理截断上限。

**Tech Stack:** Python 3.11, asyncio

---

## Bug 根因

`_run_agent_loop`（subagent.py L262-296）中 `result` 累加了所有轮次的 `StreamEvent("reply", ...)` 内容。`_run_subagent_async`（subagent.py L1334）用 `result[:2000]` 截断后推给主 Agent。如果子 Agent 中间输出多轮 reply，最终报告被截断甚至丢失。

白天测试没出问题是因为子 Agent 所有轮次 reply 累加后恰好 1997 字符（< 2000），最终报告没被截断。

---

## 修改方案

### 1. `_run_agent_loop`（subagent.py L201-296）

- 新增 `last_reply = ""` 变量
- reply 分支加 `last_reply = chunk.content`（覆盖，只保留最后一次）
- 返回值从 `(result, return_value)` 改为 `(result, return_value, last_reply)`

### 2. `call_subagent` 三处调用（subagent.py L859, L894, L927）

- 三处解包改为三元组
- 恢复路径和同步路径用 `_last_reply` 忽略
- 异步路径把 `last_reply` 存到 `SubagentRegistry` instance 上

### 3. `RunningSubagent` dataclass（subagent_registry.py）

- 加 `last_reply: str = ""` 字段

### 4. `_run_subagent_async` 完成通知（subagent.py L1332-1334）

- 用 `last_reply` 替代 `result[:2000]`
- 保留 `[:8000]` 截断上限防止极端情况
- fallback：`last_reply` 为空时用 `result[:2000]`

### 5. 测试 mock 更新

- 6 个测试文件 12 处 mock `_run_agent_loop` 返回值从二元组改为三元组

---

## 修改后代码

### `_run_agent_loop`（subagent.py L262-296）

```python
    result = ""
    last_reply = ""  # 只记录最后一次 reply 的内容（完成通知用）
    return_value = None

    while True:
        # 子 Agent 不再检查全局 stop 信号灯，只响应自己 queue 的 /stop
        try:
            chunk = next(gen)
            if isinstance(chunk, str):
                result += chunk
            elif isinstance(chunk, StreamEvent):
                if chunk.type == "reply":
                    result += chunk.content
                    last_reply = chunk.content  # 只保留最后一次 reply
                    # 子 Agent 回复文本推送到 SubagentEventBus（前端 tab 展示）
                    unique_name = getattr(handler, '_subagent_unique_name', None)
                    if unique_name:
                        try:
                            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
                            notify_subagent_event_sync(unique_name, 'reply', {'content': chunk.content})
                        except ImportError:
                            pass
                elif chunk.type in ('persist', 'system', 'tool_marker'):
                    unique_name = getattr(handler, '_subagent_unique_name', None)
                    if unique_name:
                        try:
                            from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
                            notify_subagent_event_sync(unique_name, chunk.type, {'content': chunk.content})
                        except ImportError:
                            pass
            else:
                logger.warning(f"[SubAgent] Non-string chunk from agent_runner_loop: {type(chunk).__name__}")
        except StopIteration as e:
            return_value = e.value
            break

    return result, return_value, last_reply
```

### `_run_agent_loop` 签名（L217-234）

```python
) -> tuple[str, Any, str]:
    """
    执行 agent_runner_loop 并收集结果（提取自 call_subagent）

    Returns:
        (result, return_value, last_reply) 三元组
        - result: 所有轮次 reply 累加（保留向后兼容）
        - return_value: agent_runner_loop 的返回值 dict
        - last_reply: 最后一次 reply 的内容（完成通知用）
    """
```

### `call_subagent` 三处调用

L859（恢复路径）：
```python
            result_text, return_value, _last_reply = _run_agent_loop(
```

L894（异步路径）：
```python
            result_text, return_value, last_reply = _run_agent_loop(
```

L927（同步路径）：
```python
            result_text, return_value, _last_reply = _run_agent_loop(
```

异步路径 L910-912 后加：
```python
        finally:
            # 异步路径不在这里 unregister（_run_subagent_async 的 finally 负责）
            pass
        # 异步路径：把 last_reply 存到 registry instance 供 _run_subagent_async 使用
        instance = SubagentRegistry.get(unique_name)
        if instance is not None:
            instance.last_reply = last_reply
```

### `RunningSubagent` dataclass（subagent_registry.py）

加字段：
```python
    last_reply: str = ""  # 最后一次 reply 内容（完成通知用）
```

### `_run_subagent_async` 完成通知（subagent.py L1332-1334）

```python
        # 推完成通知到 MainAgentRequestQueue 内存队列（不写 db）
        # 用 last_reply（最后一轮输出）而非 result（所有轮次累加），避免中间过程挤占最终报告
        _inst = SubagentRegistry.get(unique_name)
        _last_reply = getattr(_inst, 'last_reply', '') if _inst else ''
        _result_for_notify = _last_reply[:8000] if _last_reply else result[:2000]  # fallback + 截断保护
        completion_msg = f"[{unique_name}] 已完成，结果：{_result_for_notify}"
```

注意：`SubagentRegistry` 已在 L1314 import，无需重复 import。

### 测试 mock 更新

以下 6 个测试文件中所有 monkeypatch `_run_agent_loop` 的返回值从二元组改为三元组（加第三个空字符串元素）：

1. `tests/test_compress_quality.py` L251, L283
2. `tests/test_context_manager_bypass_at_prefix.py` L155, L186
3. `tests/test_context_overflow.py` L644, L678, L711
4. `tests/test_subagent_overflow.py` L52, L364-376, L408
5. `tests/test_sync_subagent_interaction.py` L163-166, L209-211, L255-256

每处 mock 返回值加 `''` 作为第三个元素。例如：
```python
# 修改前
monkeypatch.setattr(..., return_value=(result, return_value))
# 修改后
monkeypatch.setattr(..., return_value=(result, return_value, ''))
```

---

## 场景验证矩阵

| # | 场景 | result（累加） | last_reply | 完成通知内容 | 主 Agent 收到 | 结果 |
|---|------|---------------|------------|------------|-------------|------|
| 1a | LLM 输出 "@end 最终报告" → @end 在前 | "最终报告" | "最终报告" | "[子名] 已完成，结果：最终报告" | 最终报告 | ✓ |
| 1b | LLM 输出 "最终报告@end" → @end 在后，剥除后为空，回退原始 content | "最终报告@end" | "最终报告@end" | "[子名] 已完成，结果：最终报告@end" | 最终报告（含 @end 标记） | ✓ |
| 2 | 多轮 reply + @end | "第1轮...第N轮报告@end" | "第N轮报告@end" | "[子名] 已完成，结果：第N轮报告@end" | 最终报告 | ✓ |
| 3 | 空输出 + @end | "" | "" | "[子名] 已完成，结果：" | 空 | ✓ |
| 4 | MAX_TURNS（无 @end） | "第1轮...第N轮" | "第N轮" | "[子名] 已完成，结果：第N轮" | 最后一轮 | ✓ |
| 5 | 异常退出 | 部分累加 | 部分最后 | 不走完成通知（走 subagent_error） | N/A | ✓ |

**@end 位置说明**：`agent_loop.py` L776 `exit_content = stripped_content[at_end_idx + 4:].lstrip()` 剥除 @end 标记后的内容作为 reply yield。如果 @end 在内容末尾，剥除后为空，L778-779 回退到原始 content（含 @end）。

---

## 审查历史

### 第一轮（完整逻辑链审查）
- **P0**：6 个测试文件 12 处 mock `_run_agent_loop` 返回二元组，改三元组后会崩溃 → 新增测试 mock 更新 Task
- **P1**：移除 `[:2000]` 截断后 last_reply 可能超长 → 保留 `[:8000]` 截断上限
- **P2**：`_run_subagent_async` 重复 import SubagentRegistry → 移除，使用已有的 L1314 import
- **P2**：场景矩阵 @end 位置不准确 → 拆分为 1a/1b 两个场景，说明 @end 位置影响剥除逻辑

---

### Task 1: 修改 `_run_agent_loop` + `call_subagent` + `RunningSubagent`

**Files:**
- Modify: `agent/subagent.py:217-296`（`_run_agent_loop` 签名 + 返回值）
- Modify: `agent/subagent.py:859, 894, 927`（三处调用解包）
- Modify: `agent/subagent.py:910-912`（异步路径存 last_reply）
- Modify: `agent/subagent_registry.py`（RunningSubagent 加字段）

- [ ] **Step 1: 备份**

```bash
cd /Users/lilei/tools/ai-bot
git add -A && git commit -m "backup: before async subagent completion notification fix"
```

- [ ] **Step 2: 修改 `_run_agent_loop`**

L217 签名改为 `-> tuple[str, Any, str]:`
L218-234 docstring Returns 改为三元组说明
L262 加 `last_reply = ""`
L273 reply 分支加 `last_reply = chunk.content`
L296 返回值改为 `return result, return_value, last_reply`

- [ ] **Step 3: 修改三处调用解包**

L859: `result_text, return_value, _last_reply = _run_agent_loop(`
L894: `result_text, return_value, last_reply = _run_agent_loop(`
L927: `result_text, return_value, _last_reply = _run_agent_loop(`

- [ ] **Step 4: 异步路径存 last_reply**

L910-912 的 `finally: pass` 之后加：
```python
        # 异步路径：把 last_reply 存到 registry instance 供 _run_subagent_async 使用
        instance = SubagentRegistry.get(unique_name)
        if instance is not None:
            instance.last_reply = last_reply
```

- [ ] **Step 5: RunningSubagent 加字段**

`agent/subagent_registry.py` 的 `RunningSubagent` dataclass 加：
```python
    last_reply: str = ""  # 最后一次 reply 内容（完成通知用）
```

- [ ] **Step 6: 语法检查**

Run: `python/bin/python -c "import ast; ast.parse(open('agent/subagent.py').read()); ast.parse(open('agent/subagent_registry.py').read()); print('OK')"`
Expected: OK

### Task 2: 修改 `_run_subagent_async` 完成通知

**Files:**
- Modify: `agent/subagent.py:1332-1334`

- [ ] **Step 1: 替换完成通知**

L1332-1334 替换为：
```python
        # 推完成通知到 MainAgentRequestQueue 内存队列（不写 db）
        # 用 last_reply（最后一轮输出）而非 result（所有轮次累加），避免中间过程挤占最终报告
        _inst = SubagentRegistry.get(unique_name)
        _last_reply = getattr(_inst, 'last_reply', '') if _inst else ''
        _result_for_notify = _last_reply[:8000] if _last_reply else result[:2000]  # fallback + 截断保护
        completion_msg = f"[{unique_name}] 已完成，结果：{_result_for_notify}"
```

注意：`SubagentRegistry` 已在 L1314 import，无需重复 import。

- [ ] **Step 2: 语法检查**

Run: `python/bin/python -c "import ast; ast.parse(open('agent/subagent.py').read()); print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add agent/subagent.py agent/subagent_registry.py
git commit -m "fix: async subagent completion notification uses last_reply instead of accumulated result[:2000]"
```

### Task 3: 更新测试 mock

**Files:**
- Modify: `tests/test_compress_quality.py`（L251, L283）
- Modify: `tests/test_context_manager_bypass_at_prefix.py`（L155, L186）
- Modify: `tests/test_context_overflow.py`（L644, L678, L711）
- Modify: `tests/test_subagent_overflow.py`（L52, L364-376, L408）
- Modify: `tests/test_sync_subagent_interaction.py`（L163-166, L209-211, L255-256）

- [ ] **Step 1: 更新所有 mock 返回值**

每个文件中所有 monkeypatch `_run_agent_loop` 的 `return_value` 从二元组 `(result, return_value)` 改为三元组 `(result, return_value, '')`。

- [ ] **Step 2: 语法检查**

Run: `python/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['tests/test_compress_quality.py', 'tests/test_context_manager_bypass_at_prefix.py', 'tests/test_context_overflow.py', 'tests/test_subagent_overflow.py', 'tests/test_sync_subagent_interaction.py']]; print('OK')"`
Expected: OK

- [ ] **Step 3: 提交**

```bash
git add tests/
git commit -m "test: update _run_agent_loop mocks from 2-tuple to 3-tuple"
```

### Task 4: 运行测试

- [ ] **Step 1: 运行拦截测试**

Run: `python/bin/python -m pytest tests/test_at_prefix_interception.py -x -q`
Expected: 27 passed

- [ ] **Step 2: 运行受影响的测试文件**

Run: `python/bin/python -m pytest tests/test_compress_quality.py tests/test_context_manager_bypass_at_prefix.py tests/test_context_overflow.py tests/test_subagent_overflow.py tests/test_sync_subagent_interaction.py -x -q`
Expected: all passed

### Task 5: 端到端验证

- [ ] **Step 1: 启动应用**

```bash
cd /Users/lilei/tools/ai-bot
./niu
```

- [ ] **Step 2: 测试异步子 Agent**

触发异步子 Agent（如浏览器测试任务），确认：
- 子 Agent tab 正常显示工作过程
- 子 Agent 完成后，主 Agent 收到的完成通知是最终报告（不是中间过程）
- 主 Agent 能正确向用户汇报子 Agent 的最终结果
