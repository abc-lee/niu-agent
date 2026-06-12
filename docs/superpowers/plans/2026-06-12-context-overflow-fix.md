# 上下文溢出检测修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复上下文溢出检测逻辑——改为 Claude Code 模式：不在 80% 主动退出，而是在 LLM API 返回 context_length_exceeded 错误时才触发压缩。强制压缩时 context-manager 子 Agent 关闭 FIFO。

**Architecture:** 1) 移除 agent_loop.py 中 80% 主动溢出退出逻辑，改为仅记录警告继续执行；2) 在 litellm_adapter.py 中捕获 `context_length_exceeded` 错误，返回特殊标记；3) agent_loop 检测到该标记后返回 CONTEXT_OVERFLOW 触发强制压缩；4) call_subagent 增加 `context_fifo_threshold` 参数，force 模式传 0 关闭 FIFO；5) 子 Agent 的 FIFO 在每轮开始时先于溢出检测执行。

**Tech Stack:** Python, SQLite, litellm

---

## 修改文件

| 操作 | 文件 | 说明 |
|------|------|------|
| Modify | `agent/generic/agent_loop.py:183-203` | 移除 80% 主动退出，改为警告 + 继续执行；FIFO 移到溢出检测之前 |
| Modify | `agent/generic/litellm_adapter.py:455-495` | 捕获 `context_length_exceeded` 错误，返回 CONTEXT_OVERFLOW 标记 |
| Modify | `agent/generic/llmcore.py` | MockResponse 增加 `context_overflow` 属性 |
| Modify | `agent/subagent.py:353-359` | call_subagent 增加 `context_fifo_threshold` 可选参数 |
| Modify | `niu_api/compat.py:1609-1615` | force 模式调用 context-manager 时传 `context_fifo_threshold=0` |
| Create | `tests/test_context_overflow.py` | TDD 测试 |

---

## 核心设计决策

**1. 主 Agent 不主动退出：** 移除 agent_loop.py 第 183-203 行的 80% 溢出退出逻辑。改为仅打印警告日志，继续让 LLM 处理。只有 LLM API 真正返回 `context_length_exceeded` 错误时，才触发 CONTEXT_OVERFLOW 退出 + 强制压缩。

**2. 子 Agent FIFO 先于溢出检测：** 将 FIFO 截断逻辑从每轮结束后（第 423 行）移到每轮开始时、LLM 调用之前。这样 FIFO 先截断旧消息，然后溢出检测看到的是截断后的 token 量。

**3. 强制压缩关闭 FIFO：** call_subagent 增加 `context_fifo_threshold` 可选参数，force 模式调用 context-manager 时传 0，关闭 FIFO 截断。因为它只有一轮，FIFO 会截断掉需要压缩的信息。

**4. LLM API 错误驱动压缩：** 在 litellm_adapter.py 中捕获 `BadRequestError` / `context_length_exceeded`，在 MockResponse 上设置 `context_overflow=True` 标记。agent_loop 检测到该标记后返回 CONTEXT_OVERFLOW。

---

### Task 1: 移除主 Agent 80% 主动溢出退出，改为警告继续

**Files:**
- Modify: `agent/generic/agent_loop.py:183-203`

- [ ] **Step 1: 写测试验证当前行为（80% 退出）**

```python
# tests/test_context_overflow.py
"""TDD 测试：上下文溢出检测逻辑"""
import pytest
from unittest.mock import MagicMock, patch
from agent.generic.agent_loop import agent_runner_loop
from agent.generic.llmcore import MockResponse, MockToolCall


def _make_handler():
    """创建最小 handler mock"""
    handler = MagicMock()
    handler.max_turns = 40
    handler._done_hooks = []
    handler.process_tool_call = MagicMock(return_value=None)
    handler.get_tool_schema = MagicMock(return_value=[])
    return handler


def test_overflow_at_80_percent_exits_with_context_overflow():
    """当前行为：上下文超过 80% 时返回 CONTEXT_OVERFLOW"""
    handler = _make_handler()
    system_prompt = "You are a test assistant."
    user_input = "test"
    # 模拟大量历史消息使 token 超过 80%
    big_history = []
    for i in range(50):
        big_history.append({"role": "user", "content": "x" * 2000})
        big_history.append({"role": "assistant", "content": "y" * 2000})

    results = []
    for event in agent_runner_loop(
        client=MagicMock(),
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=[],
        max_turns=40,
        context_window_tokens=200000,
        context_fifo_threshold=0,
        history=big_history,
    ):
        results.append(event)

    # 当前行为：返回 CONTEXT_OVERFLOW
    return_values = [r for r in results if isinstance(r, dict) and r.get("result") == "CONTEXT_OVERFLOW"]
    assert len(return_values) > 0, "Expected CONTEXT_OVERFLOW when tokens exceed 80%"


def test_no_proactive_exit_after_fix():
    """修复后行为：上下文超过 80% 不主动退出，继续执行 LLM 调用"""
    handler = _make_handler()
    # Mock LLM 返回正常响应
    mock_client = MagicMock()
    mock_response = MockResponse(content="OK", tool_calls=[])
    mock_client.chat.return_value = iter([mock_response])

    system_prompt = "You are a test assistant."
    user_input = "test"
    big_history = []
    for i in range(50):
        big_history.append({"role": "user", "content": "x" * 2000})
        big_history.append({"role": "assistant", "content": "y" * 2000})

    results = []
    for event in agent_runner_loop(
        client=mock_client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=[],
        max_turns=1,
        context_window_tokens=200000,
        context_fifo_threshold=0,
        history=big_history,
    ):
        results.append(event)

    # 修复后：不应该返回 CONTEXT_OVERFLOW，而是正常完成
    return_values = [r for r in results if isinstance(r, dict) and r.get("result") == "CONTEXT_OVERFLOW"]
    assert len(return_values) == 0, "Should NOT proactively exit at 80% after fix"
    # 应该调用了 LLM
    assert mock_client.chat.called, "Should have called LLM even when tokens > 80%"
```

- [ ] **Step 2: 运行测试确认 test_overflow_at_80_percent_exits 通过（验证当前 bug 行为）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py::test_overflow_at_80_percent_exits_with_context_overflow -v
```

Expected: PASS（确认当前 80% 退出行为存在）

- [ ] **Step 3: 运行测试确认 test_no_proactive_exit_after_fix 失败（验证修复目标）**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py::test_no_proactive_exit_after_fix -v
```

Expected: FAIL（因为当前代码在 80% 就退出了，不会调用 LLM）

- [ ] **Step 4: 修改 agent_loop.py — 将溢出退出改为警告继续**

将第 183-203 行：

```python
        # 上下文溢出保护：检查 token 使用率
        if context_window_tokens > 0:
            current_tokens = count_messages_tokens(messages)
            usage_ratio = current_tokens / context_window_tokens
            if usage_ratio > warning_threshold:
                logger.warning(f"[Overflow] Context {current_tokens}/{context_window_tokens} tokens ({usage_ratio:.1%}) exceeds {warning_threshold:.0%} threshold")
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                # V4: 通知前端进入空闲状态
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                return {
                    "result": "CONTEXT_OVERFLOW",
                    "data": {
                        "overflow": True,
                        "turns_completed": turn - 1,
                        "tokens_used": current_tokens,
                        "tokens_limit": context_window_tokens,
                    },
                    "messages": messages,
                }
```

替换为：

```python
        # 上下文使用率监控（仅警告，不主动退出 — 由 LLM API 报错驱动压缩）
        if context_window_tokens > 0:
            current_tokens = count_messages_tokens(messages)
            usage_ratio = current_tokens / context_window_tokens
            if usage_ratio > warning_threshold:
                logger.warning(f"[Context] High usage {current_tokens}/{context_window_tokens} tokens ({usage_ratio:.1%}), will continue and let LLM API decide")
```

- [ ] **Step 5: 运行测试确认两个测试都通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py -v
```

Expected: test_overflow_at_80_percent_exits 可能需要调整（因为行为变了），test_no_proactive_exit_after_fix PASS

- [ ] **Step 6: 语法验证**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); print('Syntax OK')"
```

- [ ] **Step 7: 提交**

```bash
git add agent/generic/agent_loop.py tests/test_context_overflow.py
git commit -m "fix: remove proactive 80% overflow exit — let LLM API decide, only compress on actual error"
```

---

### Task 2: 捕获 LLM API context_length_exceeded 错误，触发 CONTEXT_OVERFLOW

**Files:**
- Modify: `agent/generic/litellm_adapter.py:455-495`
- Modify: `agent/generic/llmcore.py` — MockResponse 增加 context_overflow 属性
- Modify: `agent/generic/agent_loop.py` — 检测 context_overflow 标记

- [ ] **Step 1: 写测试验证 context_length_exceeded 触发 CONTEXT_OVERFLOW**

在 `tests/test_context_overflow.py` 中添加：

```python
def test_context_length_exceeded_triggers_overflow():
    """LLM API 返回 context_length_exceeded 错误时，触发 CONTEXT_OVERFLOW 退出"""
    handler = _make_handler()
    # Mock LLM 抛出 context_length_exceeded 错误
    mock_client = MagicMock()
    mock_response = MockResponse(content="", tool_calls=[], context_overflow=True)
    mock_client.chat.return_value = iter([mock_response])

    system_prompt = "You are a test assistant."
    user_input = "test"

    results = []
    for event in agent_runner_loop(
        client=mock_client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=[],
        max_turns=1,
        context_window_tokens=200000,
        context_fifo_threshold=0,
        history=[],
    ):
        results.append(event)

    return_values = [r for r in results if isinstance(r, dict) and r.get("result") == "CONTEXT_OVERFLOW"]
    assert len(return_values) > 0, "Should return CONTEXT_OVERFLOW when LLM returns context_length_exceeded"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py::test_context_length_exceeded_triggers_overflow -v
```

Expected: FAIL（MockResponse 还没有 context_overflow 属性）

- [ ] **Step 3: MockResponse 增加 context_overflow 属性**

在 `agent/generic/llmcore.py` 中，找到 MockResponse 类定义，添加 `context_overflow` 属性：

```python
class MockResponse:
    def __init__(self, thinking="", content="", tool_calls=None, usage=None, context_overflow=False):
        self.thinking = thinking
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage = usage or MockUsage()
        self.context_overflow = context_overflow
```

- [ ] **Step 4: litellm_adapter.py 捕获 context_length_exceeded**

在 `agent/generic/litellm_adapter.py` 第 455 行的 `except Exception as e:` 中，在 socket 错误处理之前，增加 `context_length_exceeded` 检测：

将第 455-496 行的异常处理改为：

```python
        except Exception as e:
            error_msg = str(e)

            # 检测 context_length_exceeded 错误 — 设置标记让 agent_loop 触发强制压缩
            is_context_overflow = (
                "context_length_exceeded" in error_msg
                or "context window" in error_msg.lower()
                or "prompt is too long" in error_msg.lower()
                or "maximum context length" in error_msg.lower()
            )
            if is_context_overflow:
                logger.warning(f"[STREAM] Context length exceeded: {e}")
                mock_resp = MockResponse(
                    content=full_content or "",
                    tool_calls=tool_calls,
                    context_overflow=True,
                )
                yield mock_resp
                return

            is_socket_error = "10038" in error_msg or "10054" in error_msg or "non-socket" in error_msg.lower()

            if is_socket_error and not full_content:
                # WinError 10038/10054: Windows socket 在流式传输中被关闭，尝试非流式 fallback
                logger.warning(f"[STREAM] Socket error with empty content, trying non-stream fallback: {e}")
                try:
                    fallback_params = {**request_params, "stream": False}
                    fallback_response = litellm.completion(**fallback_params)
                    if fallback_response and fallback_response.choices:
                        choice = fallback_response.choices[0]
                        full_content = choice.message.content or ""
                        if full_content:
                            yield full_content
                        if hasattr(choice.message, "reasoning_content") and choice.message.reasoning_content:
                            reasoning_content = choice.message.reasoning_content
                        if hasattr(choice.message, "tool_calls") and choice.message.tool_calls:
                            for tc in choice.message.tool_calls:
                                tc_args = {}
                                if hasattr(tc, 'function') and tc.function:
                                    if hasattr(tc.function, 'arguments') and tc.function.arguments:
                                        try:
                                            tc_args = json.loads(tc.function.arguments)
                                        except json.JSONDecodeError:
                                            tc_args = {}
                                    tool_calls.append(MockToolCall(
                                        name=getattr(tc.function, "name", ""),
                                        args=tc_args,
                                        id=getattr(tc, "id", f"call_fallback_{len(tool_calls)}"),
                                    ))
                        if hasattr(fallback_response, 'usage') and fallback_response.usage:
                            usage = fallback_response.usage
                        logger.info(f"[STREAM] Non-stream fallback succeeded ({len(full_content)} chars, {len(tool_calls)} tool_calls)")
                except Exception as fb_err:
                    logger.error(f"[STREAM] Non-stream fallback also failed: {fb_err}")
            else:
                logger.error(f"[STREAM] Stream error: {e}")
                if full_content:
                    logger.warning(f"[STREAM] Using partial content ({len(full_content)} chars)")
```

- [ ] **Step 5: agent_loop.py 检测 context_overflow 标记**

在 agent_loop.py 中，LLM 响应处理之后（`response = yield from response_gen` 或 `response = exhaust(response_gen)` 之后），增加 context_overflow 检测：

在 `yield StreamEvent("reply", content)` 之前（约第 229 行），添加：

```python
            # 检测 LLM 返回的 context_length_exceeded 标记
            if hasattr(response, 'context_overflow') and response.context_overflow:
                logger.warning(f"[Overflow] LLM API returned context_length_exceeded, triggering CONTEXT_OVERFLOW")
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                clear_stop()
                yield StreamEvent("system", "chat_idle")
                return {
                    "result": "CONTEXT_OVERFLOW",
                    "data": {
                        "overflow": True,
                        "turns_completed": turn - 1,
                        "tokens_used": count_messages_tokens(messages),
                        "tokens_limit": context_window_tokens,
                    },
                    "messages": messages,
                }
```

- [ ] **Step 6: 运行测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py -v
```

- [ ] **Step 7: 语法验证**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); ast.parse(open('agent/generic/litellm_adapter.py').read()); ast.parse(open('agent/generic/llmcore.py').read()); print('All Syntax OK')"
```

- [ ] **Step 8: 提交**

```bash
git add agent/generic/llmcore.py agent/generic/litellm_adapter.py agent/generic/agent_loop.py tests/test_context_overflow.py
git commit -m "feat: handle context_length_exceeded from LLM API — trigger CONTEXT_OVERFLOW on real error, not proactive 80% exit"
```

---

### Task 3: FIFO 截断移到溢出检测之前执行

**Files:**
- Modify: `agent/generic/agent_loop.py`

- [ ] **Step 1: 写测试验证 FIFO 在溢出检测之前执行**

在 `tests/test_context_overflow.py` 中添加：

```python
def test_fifo_truncates_before_overflow_check():
    """FIFO 截断在每轮开始时先于溢出检测执行，避免子 Agent 被提前退出"""
    handler = _make_handler()
    mock_client = MagicMock()
    mock_response = MockResponse(content="OK", tool_calls=[])
    mock_client.chat.return_value = iter([mock_response])

    system_prompt = "You are a test assistant."
    user_input = "test"
    # 50 条历史消息，每条约 4000 tokens → 约 200K tokens
    big_history = []
    for i in range(50):
        big_history.append({"role": "user", "content": "x" * 2000})
        big_history.append({"role": "assistant", "content": "y" * 2000})

    # FIFO 阈值设为 75% (150K)，应该在溢出检测之前截断
    results = []
    for event in agent_runner_loop(
        client=mock_client,
        system_prompt=system_prompt,
        user_input=user_input,
        handler=handler,
        tools_schema=[],
        max_turns=1,
        context_window_tokens=200000,
        context_fifo_threshold=150000,  # 75% FIFO
        history=big_history,
    ):
        results.append(event)

    # FIFO 应该截断了旧消息，然后 LLM 被正常调用
    assert mock_client.chat.called, "LLM should be called after FIFO truncation"
    overflow_returns = [r for r in results if isinstance(r, dict) and r.get("result") == "CONTEXT_OVERFLOW"]
    assert len(overflow_returns) == 0, "Should NOT overflow after FIFO truncation"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py::test_fifo_truncates_before_overflow_check -v
```

Expected: FAIL（当前 FIFO 在每轮结束后才执行）

- [ ] **Step 3: 将 FIFO 截断逻辑从每轮结束后移到每轮开始时**

在 `agent_loop.py` 的 `while turn < handler.max_turns:` 循环中，将 FIFO 逻辑移到溢出检测之前：

将循环体开头改为（stop check → FIFO → overflow check → LLM call）：

```python
    while turn < handler.max_turns:
        turn += 1
        # --- Stop flag check ---
        if is_stop_requested():
            logger.info("[AgentLoop] Stop requested, exiting loop")
            clear_stop()
            yield StreamEvent("system", "chat_idle")
            return {"result": "STOPPED", "messages": messages}

        # FIFO 上下文截断：每轮开始时先截断，再检查使用率
        # 保护 messages[0](system) 和 messages[1](初始task)
        if context_fifo_threshold > 0 and len(messages) > 2:
            current_tokens = count_messages_tokens(messages)
            if current_tokens > context_fifo_threshold:
                removed = 0
                while len(messages) > 2 and current_tokens > context_fifo_threshold:
                    first = messages[2]
                    messages.pop(2)
                    removed += 1
                    if first.get("role") == "assistant" and first.get("tool_calls"):
                        while len(messages) > 2 and messages[2].get("role") == "tool":
                            messages.pop(2)
                            removed += 1
                    current_tokens = count_messages_tokens(messages)
                if removed > 0:
                    logger.info(f"[FIFO] Context truncation: removed {removed} oldest messages, "
                                f"tokens {current_tokens}/{context_fifo_threshold}")

        # 上下文使用率监控（仅警告，不主动退出 — 由 LLM API 报错驱动压缩）
        if context_window_tokens > 0:
            current_tokens = count_messages_tokens(messages)
            usage_ratio = current_tokens / context_window_tokens
            if usage_ratio > warning_threshold:
                logger.warning(f"[Context] High usage {current_tokens}/{context_window_tokens} tokens ({usage_ratio:.1%}), will continue and let LLM API decide")
```

同时**删除**原来每轮结束后的 FIFO 代码（第 423-442 行）。

- [ ] **Step 4: 运行测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py -v
```

- [ ] **Step 5: 语法验证**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); print('Syntax OK')"
```

- [ ] **Step 6: 提交**

```bash
git add agent/generic/agent_loop.py tests/test_context_overflow.py
git commit -m "fix: move FIFO truncation before overflow check — truncate first, then let LLM decide"
```

---

### Task 4: call_subagent 增加 context_fifo_threshold 参数，force 模式关闭 FIFO

**Files:**
- Modify: `agent/subagent.py:353-359`
- Modify: `niu_api/compat.py:1609-1615`

- [ ] **Step 1: 写测试验证 force 模式下 context-manager 不做 FIFO 截断**

在 `tests/test_context_overflow.py` 中添加：

```python
def test_call_subagent_with_fifo_disabled():
    """call_subagent 传 context_fifo_threshold=0 时，子 Agent 不做 FIFO 截断"""
    from agent.subagent import call_subagent
    # 仅验证参数传递，不实际调用子 Agent
    # 实际集成测试在启动程序后进行
    import inspect
    sig = inspect.signature(call_subagent)
    assert "context_fifo_threshold" in sig.parameters, "call_subagent should accept context_fifo_threshold parameter"
    assert sig.parameters["context_fifo_threshold"].default is not inspect.Parameter.empty or sig.parameters["context_fifo_threshold"].default == -1, "Should have a default value"
```

- [ ] **Step 2: 修改 call_subagent 函数签名**

将 `agent/subagent.py:353-359`：

```python
def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
    history: Optional[list] = None,
) -> str:
```

改为：

```python
def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
    history: Optional[list] = None,
    context_fifo_threshold: int = -1,  # -1: use default (75% of context window), 0: disable FIFO, >0: custom token budget
) -> str:
```

- [ ] **Step 3: 修改 call_subagent 内部的 FIFO 阈值计算**

将 `agent/subagent.py:430-432`：

```python
    context_window_tokens = _read_context_window_tokens()
    # FIFO 截断阈值：75% 的上下文窗口，比溢出检测(warningThreshold)低，留出缓冲空间
    context_fifo_threshold = int(context_window_tokens * 0.75)
```

改为：

```python
    context_window_tokens = _read_context_window_tokens()
    # FIFO 截断阈值：-1 使用默认 75%，0 关闭 FIFO，>0 使用自定义值
    if context_fifo_threshold == -1:
        context_fifo_threshold = int(context_window_tokens * 0.75)
```

注意：这里 `context_fifo_threshold` 是函数参数名，和局部变量名冲突。需要将局部变量改名避免冲突。将调用 `_run_agent_loop` 时的参数名改清楚：

```python
    context_window_tokens = _read_context_window_tokens()
    # FIFO 截断阈值：-1 使用默认 75%，0 关闭 FIFO，>0 使用自定义值
    fifo_threshold = context_fifo_threshold  # 函数参数
    if fifo_threshold == -1:
        fifo_threshold = int(context_window_tokens * 0.75)
```

然后在 `_run_agent_loop` 调用中使用 `fifo_threshold`：

```python
    result_text, return_value = _run_agent_loop(
        client=client,
        system_prompt=system_prompt,
        user_input=task,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=20,
        initial_user_content=task,
        context_window_tokens=context_window_tokens,
        context_fifo_threshold=fifo_threshold,
        history=history,
    )
```

- [ ] **Step 4: force 模式调用 context-manager 时传 context_fifo_threshold=0**

将 `niu_api/compat.py:1609-1615`：

```python
            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                )
```

改为：

```python
            def run_context_manager_force():
                return call_subagent(
                    agent_name="context-manager",
                    task=prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    context_fifo_threshold=0,  # 强制压缩：关闭 FIFO，保留全部信息供压缩决策
                )
```

- [ ] **Step 5: 运行测试确认通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py -v
```

- [ ] **Step 6: 语法验证**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -c "import ast; ast.parse(open('agent/subagent.py').read()); ast.parse(open('niu_api/compat.py').read()); print('All Syntax OK')"
```

- [ ] **Step 7: 提交**

```bash
git add agent/subagent.py niu_api/compat.py tests/test_context_overflow.py
git commit -m "feat: add context_fifo_threshold param to call_subagent — force mode disables FIFO for context-manager"
```

---

### Task 5: 集成测试 — 启动程序验证修复有效

**Files:**
- Modify: `tests/test_context_overflow.py` — 增加集成测试

- [ ] **Step 1: 写集成测试脚本**

在 `tests/test_context_overflow.py` 中添加：

```python
def test_integration_force_compress_without_fifo():
    """集成测试：模拟强制压缩场景 — context-manager 子 Agent 不做 FIFO，不主动退出

    场景：
    1. 上下文达到 184K tokens（92%）
    2. 主 Agent 不主动退出（不再 80% 退出）
    3. LLM API 返回 context_length_exceeded
    4. 触发 CONTEXT_OVERFLOW → 强制压缩
    5. context-manager 子 Agent 关闭 FIFO（传 0）
    6. 子 Agent 正常执行 write 工具生成 compress_plan.json
    """
    # 此测试验证数据流路径，不启动真实 LLM
    # 真实集成测试需启动程序手动验证

    # 1. 验证主 Agent 不主动退出
    handler = _make_handler()
    mock_client = MagicMock()
    mock_response = MockResponse(content="OK", tool_calls=[])
    mock_client.chat.return_value = iter([mock_response])

    big_history = []
    for i in range(50):
        big_history.append({"role": "user", "content": "x" * 2000})
        big_history.append({"role": "assistant", "content": "y" * 2000})

    results = []
    for event in agent_runner_loop(
        client=mock_client,
        system_prompt="test",
        user_input="test",
        handler=handler,
        tools_schema=[],
        max_turns=1,
        context_window_tokens=200000,
        context_fifo_threshold=0,
        history=big_history,
    ):
        results.append(event)

    overflow_returns = [r for r in results if isinstance(r, dict) and r.get("result") == "CONTEXT_OVERFLOW"]
    assert len(overflow_returns) == 0, "Main agent should NOT proactively exit at 80%+"

    # 2. 验证 context_length_exceeded 触发 CONTEXT_OVERFLOW
    mock_client2 = MagicMock()
    mock_response2 = MockResponse(content="", tool_calls=[], context_overflow=True)
    mock_client2.chat.return_value = iter([mock_response2])

    results2 = []
    for event in agent_runner_loop(
        client=mock_client2,
        system_prompt="test",
        user_input="test",
        handler=handler,
        tools_schema=[],
        max_turns=1,
        context_window_tokens=200000,
        context_fifo_threshold=0,
        history=[],
    ):
        results2.append(event)

    overflow_returns2 = [r for r in results2 if isinstance(r, dict) and r.get("result") == "CONTEXT_OVERFLOW"]
    assert len(overflow_returns2) > 0, "Should trigger CONTEXT_OVERFLOW when LLM returns context_length_exceeded"

    # 3. 验证 call_subagent 参数传递
    import inspect
    sig = inspect.signature(call_subagent)
    assert "context_fifo_threshold" in sig.parameters
```

- [ ] **Step 2: 运行全部测试**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/test_context_overflow.py -v
```

- [ ] **Step 3: 运行已有测试确认无回归**

```bash
cd REDACTED_USER_PATH/tools/ai-bot && python -m pytest tests/ -v --timeout=60
```

- [ ] **Step 4: 提交**

```bash
git add tests/test_context_overflow.py
git commit -m "test: add integration tests for context overflow fix"
```
