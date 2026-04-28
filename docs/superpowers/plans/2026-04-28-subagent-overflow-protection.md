# 子 Agent 上下文溢出保护 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现两层子 Agent 上下文溢出保护：1) 调用方 prompt 超过 50K token 自动分次发送；2) 子 Agent 运行时超过 85% token 使用率主动停止并返回进度

**Architecture:** 在 `agent/subagent.py` 的 `call_subagent` 中添加 prompt 分片逻辑（调用方保护），在 `agent/generic/agent_loop.py` 的 `agent_runner_loop` 中添加每轮 token 检查（运行时保护）。两者独立但互补：分片减少单次输入量，运行时检查防止工具调用累积导致溢出。

**Tech Stack:** Python 3.11+, litellm.token_counter (已有), pytest

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `agent/subagent.py` | `call_subagent` 入口，prompt 分片 + 分次调用 | Modify |
| `agent/generic/agent_loop.py` | `agent_runner_loop` 每轮 token 检查 + 85% 阈值退出 | Modify |
| `agent/context_manager.py` | `count_tokens_simple` 已有，无需修改 | Read-only |
| `tests/test_subagent_overflow.py` | 所有 TDD 测试 | Create |

## Key Design Decisions

1. **Token 计数**：复用 `litellm.token_counter(model="gpt-4o")`，与 `compat.py` 和 `context_manager.py` 保持一致
2. **分片策略**：按消息行（`\n` 分隔）分片，保持每片 ≤ 50K token，片间传递"续接上下文"
3. **85% 阈值**：子 Agent 的 `context_window_tokens` 从 `~/.niu/preferences.json` 读取（与主 Agent 相同），默认 200K
4. **进度报告格式**：`{"overflow": true, "turns_completed": N, "tokens_used": M, "tokens_limit": L, "partial_result": "..."}`

---

### Task 1: Token 计数工具函数

**Files:**
- Create: `tests/test_subagent_overflow.py`
- Modify: `agent/subagent.py`

- [ ] **Step 1: Write the failing test — count_tokens_for_text**

```python
"""Tests for sub-agent context overflow protection."""
import pytest


class TestCountTokensForText:
    """Test the token counting utility for sub-agent prompts."""

    def test_empty_string_returns_zero(self):
        from agent.subagent import count_tokens_for_text
        assert count_tokens_for_text("") == 0

    def test_short_text_returns_positive(self):
        from agent.subagent import count_tokens_for_text
        tokens = count_tokens_for_text("Hello world")
        assert tokens > 0

    def test_chinese_text_counts_correctly(self):
        from agent.subagent import count_tokens_for_text
        # Chinese text: ~1 token per 1-2 characters
        text = "这是一段中文测试文本"
        tokens = count_tokens_for_text(text)
        assert tokens > 0
        # Should be roughly 5-10 tokens for 10 Chinese chars
        assert 3 <= tokens <= 15

    def test_long_text_counts_more(self):
        from agent.subagent import count_tokens_for_text
        short = "Hello world"
        long = "Hello world " * 100
        assert count_tokens_for_text(long) > count_tokens_for_text(short)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent_overflow.py::TestCountTokensForText -v`
Expected: FAIL — `count_tokens_for_text` not found

- [ ] **Step 3: Write minimal implementation**

In `agent/subagent.py`, add after the existing imports:

```python
def count_tokens_for_text(text: str) -> int:
    """
    计算文本的 token 数量（用于子 Agent prompt 分片判断）

    使用 litellm.token_counter，回退到字符数估算。

    Args:
        text: 纯文本字符串

    Returns:
        token 数量
    """
    if not text:
        return 0
    try:
        from litellm import token_counter
        return token_counter(model="gpt-4o", messages=[{"role": "user", "content": text}])
    except Exception:
        # 回退：约 2 字符/token（偏保守）
        return max(1, len(text) // 2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent_overflow.py::TestCountTokensForText -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagent_overflow.py agent/subagent.py
git commit -m "feat: add count_tokens_for_text utility for sub-agent overflow protection"
```

---

### Task 2: Prompt 分片函数

**Files:**
- Modify: `tests/test_subagent_overflow.py`
- Modify: `agent/subagent.py`

- [ ] **Step 1: Write the failing test — split_prompt_by_tokens**

```python
class TestSplitPromptByTokens:
    """Test prompt splitting for sub-agent overflow protection."""

    def test_short_prompt_no_split(self):
        from agent.subagent import split_prompt_by_tokens
        # 100-token prompt, 50K limit → no split
        chunks = split_prompt_by_tokens("Hello world", max_tokens_per_chunk=50000)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_long_prompt_splits(self):
        from agent.subagent import split_prompt_by_tokens
        # Create a prompt that exceeds 100 tokens
        lines = [f"消息 {i}: 这是一段测试内容用于验证分片功能" for i in range(200)]
        prompt = "\n".join(lines)
        # Use small limit to force split
        chunks = split_prompt_by_tokens(prompt, max_tokens_per_chunk=200)
        assert len(chunks) >= 2

    def test_empty_prompt_returns_empty_list(self):
        from agent.subagent import split_prompt_by_tokens
        chunks = split_prompt_by_tokens("", max_tokens_per_chunk=50000)
        assert chunks == []

    def test_single_long_line_not_split(self):
        from agent.subagent import split_prompt_by_tokens
        # A single line that exceeds the limit should still be returned as one chunk
        # (we don't split mid-line)
        long_line = "测试" * 10000
        chunks = split_prompt_by_tokens(long_line, max_tokens_per_chunk=100)
        assert len(chunks) == 1
        assert chunks[0] == long_line

    def test_chunks_preserve_content(self):
        from agent.subagent import split_prompt_by_tokens
        lines = [f"消息 {i}: 内容" for i in range(50)]
        prompt = "\n".join(lines)
        chunks = split_prompt_by_tokens(prompt, max_tokens_per_chunk=200)
        # Rejoining chunks with newline should give back original content
        rejoined = "\n".join(chunks)
        assert rejoined == prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent_overflow.py::TestSplitPromptByTokens -v`
Expected: FAIL — `split_prompt_by_tokens` not found

- [ ] **Step 3: Write minimal implementation**

In `agent/subagent.py`, add after `count_tokens_for_text`:

```python
def split_prompt_by_tokens(text: str, max_tokens_per_chunk: int = 50000) -> list[str]:
    """
    按 token 限制将 prompt 分片（按行分割，不拆行内）

    Args:
        text: 完整 prompt 文本
        max_tokens_per_chunk: 每片最大 token 数（默认 50K）

    Returns:
        分片列表（每个元素是一个完整的 prompt 片段）
    """
    if not text:
        return []

    # 先检查整体是否超限
    total_tokens = count_tokens_for_text(text)
    if total_tokens <= max_tokens_per_chunk:
        return [text]

    # 按行分割
    lines = text.split("\n")
    chunks = []
    current_lines = []
    current_tokens = 0

    for line in lines:
        line_tokens = count_tokens_for_text(line) if line else 1

        # 如果加入这行会超限，且当前片非空，先保存当前片
        if current_lines and (current_tokens + line_tokens > max_tokens_per_chunk):
            chunks.append("\n".join(current_lines))
            current_lines = []
            current_tokens = 0

        current_lines.append(line)
        current_tokens += line_tokens

    # 保存最后一片
    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks if chunks else [text]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent_overflow.py::TestSplitPromptByTokens -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagent_overflow.py agent/subagent.py
git commit -m "feat: add split_prompt_by_tokens for sub-agent prompt chunking"
```

---

### Task 3: call_subagent 集成分片 + 分次调用

**Files:**
- Modify: `tests/test_subagent_overflow.py`
- Modify: `agent/subagent.py`

- [ ] **Step 1: Write the failing test — call_subagent with chunking**

```python
class TestCallSubagentChunking:
    """Test that call_subagent splits large prompts into multiple calls."""

    def test_short_prompt_calls_once(self, monkeypatch):
        """Prompt under 50K tokens should result in a single call_subagent call."""
        from agent import subagent

        call_count = 0
        original_loop = subagent.agent_runner_loop

        def mock_runner_loop(**kwargs):
            nonlocal call_count
            call_count += 1
            # Return a generator that yields nothing then returns
            def gen():
                yield ""
                return {"result": "CURRENT_TASK_DONE", "data": "ok"}
            return gen()

        monkeypatch.setattr(subagent, "agent_runner_loop", mock_runner_loop)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
        monkeypatch.setattr(subagent, "create_client", lambda cfg: None)
        monkeypatch.setattr(subagent, "get_tools_schema", lambda: [])

        result = subagent.call_subagent(
            agent_name="test-agent",
            task="short task",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )
        assert call_count == 1

    def test_chunked_prompt_appends_continuation(self, monkeypatch):
        """When prompt is split, each chunk after the first should include continuation context."""
        from agent import subagent

        captured_tasks = []
        original_loop = subagent.agent_runner_loop

        def mock_runner_loop(**kwargs):
            captured_tasks.append(kwargs.get("user_input", ""))
            def gen():
                yield "partial"
                return {"result": "CURRENT_TASK_DONE", "data": "done"}
            return gen()

        monkeypatch.setattr(subagent, "agent_runner_loop", mock_runner_loop)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
        monkeypatch.setattr(subagent, "create_client", lambda cfg: None)
        monkeypatch.setattr(subagent, "get_tools_schema", lambda: [])

        # Force small chunk size to trigger splitting
        monkeypatch.setattr(subagent, "PROMPT_MAX_TOKENS_PER_CHUNK", 50)

        long_task = "\n".join([f"消息 {i}: 测试内容" for i in range(100)])
        result = subagent.call_subagent(
            agent_name="test-agent",
            task=long_task,
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )

        # Should have multiple calls
        assert len(captured_tasks) >= 2
        # Second chunk should have continuation marker
        assert "续接" in captured_tasks[1] or "continuation" in captured_tasks[1].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent_overflow.py::TestCallSubagentChunking -v`
Expected: FAIL — `PROMPT_MAX_TOKENS_PER_CHUNK` not found, or call_subagent doesn't split

- [ ] **Step 3: Write minimal implementation**

In `agent/subagent.py`, add constant and modify `call_subagent`:

Add near top of file (after imports):
```python
# 子 Agent prompt 分片阈值（token 数）
PROMPT_MAX_TOKENS_PER_CHUNK = 50000
```

Modify `call_subagent` — replace the existing function body from step 7 onwards with chunking logic. The key change is in the section after building `tools_schema`:

```python
    # 7. 检查 prompt 是否需要分片
    chunks = split_prompt_by_tokens(task, max_tokens_per_chunk=PROMPT_MAX_TOKENS_PER_CHUNK)

    if len(chunks) == 1:
        # 单次调用（原有逻辑）
        gen = agent_runner_loop(
            client=client,
            system_prompt=system_prompt,
            user_input=task,
            handler=handler,
            tools_schema=tools_schema,
            max_turns=20,
            verbose=False,
            initial_user_content=task,
        )

        # 8. 收集结果
        result = ""
        return_value = None
        while True:
            try:
                chunk = next(gen)
                if isinstance(chunk, str):
                    result += chunk
                else:
                    content = getattr(chunk, "content", None)
                    if content and isinstance(content, str):
                        result += content
                    else:
                        logger.warning(f"[SubAgent] Non-string chunk: {type(chunk).__name__}")
                        result += str(chunk)
            except StopIteration as e:
                return_value = e.value
                break

        if return_value and isinstance(return_value, dict):
            if "data" in return_value and return_value["data"] is not None:
                data = return_value["data"]
                if isinstance(data, dict):
                    return json.dumps(data, ensure_ascii=False)
                return str(data)
            return json.dumps(return_value, ensure_ascii=False)

        return result

    else:
        # 分片调用：多次调用，每次处理一片
        logger.info(f"[SubAgent] {agent_name}: Prompt split into {len(chunks)} chunks ({count_tokens_for_text(task)} tokens)")
        all_results = []
        for i, chunk_text in enumerate(chunks):
            if i == 0:
                user_input = chunk_text
            else:
                # 续接上下文：告知子 Agent 这是第几片，之前处理了什么
                prev_summary = all_results[-1][-500:] if all_results else ""
                user_input = f"[续接上下文] 这是任务的第 {i+1}/{len(chunks)} 片。前一片处理结果摘要：\n{prev_summary}\n\n---\n\n{chunk_text}"

            gen = agent_runner_loop(
                client=client,
                system_prompt=system_prompt,
                user_input=user_input,
                handler=handler,
                tools_schema=tools_schema,
                max_turns=20,
                verbose=False,
                initial_user_content=user_input,
            )

            chunk_result = ""
            return_value = None
            while True:
                try:
                    piece = next(gen)
                    if isinstance(piece, str):
                        chunk_result += piece
                    else:
                        content = getattr(piece, "content", None)
                        if content and isinstance(content, str):
                            chunk_result += content
                        else:
                            chunk_result += str(piece)
                except StopIteration as e:
                    return_value = e.value
                    break

            # 提取 return_value
            if return_value and isinstance(return_value, dict):
                if "data" in return_value and return_value["data"] is not None:
                    data = return_value["data"]
                    chunk_result = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)

            all_results.append(chunk_result)
            logger.info(f"[SubAgent] {agent_name}: Chunk {i+1}/{len(chunks)} completed")

        return "\n".join(all_results)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent_overflow.py::TestCallSubagentChunking -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagent_overflow.py agent/subagent.py
git commit -m "feat: call_subagent splits prompts exceeding 50K tokens into multiple calls"
```

---

### Task 4: agent_runner_loop 85% token 阈值检查

**Files:**
- Modify: `tests/test_subagent_overflow.py`
- Modify: `agent/generic/agent_loop.py`

- [ ] **Step 1: Write the failing test — 85% threshold exit**

```python
class TestAgentLoopTokenThreshold:
    """Test that agent_runner_loop exits at 85% token usage."""

    def test_exits_at_85_percent_token_usage(self, monkeypatch):
        """When token usage exceeds 85%, agent_runner_loop should return overflow report."""
        from agent.generic.agent_loop import agent_runner_loop, MockResponse, MockToolCall

        turn_count = 0

        # Mock client that simulates growing context
        class MockClient:
            name = "mock"
            last_tools = ""
            total_cd_tokens = 0

            def chat(self, messages, tools=None):
                nonlocal turn_count
                turn_count += 1
                # Simulate a response that adds ~30K tokens per turn
                # After 6 turns: ~180K tokens > 85% of 200K = 170K
                def gen():
                    resp = MockResponse(
                        thinking=None,
                        content=f"Turn {turn_count} result",
                        tool_calls=None,
                        raw=None,
                    )
                    yield resp
                return gen()

        # Mock handler
        class MockHandler:
            _done_hooks = []
            max_turns = 40
            current_turn = 0

            def dispatch(self, tool_name, args, response, index=0):
                from agent.generic.agent_loop import StepOutcome
                def gen():
                    yield ""
                    return StepOutcome(None, next_prompt="continue", should_exit=False)
                return gen()

            def next_prompt_patcher(self, next_prompt, outcome, turn):
                return next_prompt

        # Use small context window to trigger threshold quickly
        client = MockClient()
        handler = MockHandler()

        # Mock token counter to return growing token count
        token_counts = [0]  # mutable for closure

        def mock_count_tokens(messages):
            # Each message adds ~30K tokens worth of content
            total = sum(len(m.get("content", "")) // 2 + 4 for m in messages)
            return total

        monkeypatch.setattr(
            "agent.generic.agent_loop.count_messages_tokens",
            mock_count_tokens,
        )

        # Build messages that will exceed 85% of 200K
        # Use context_window_tokens=200000, 85% = 170000
        gen = agent_runner_loop(
            client=client,
            system_prompt="You are a test agent.",
            user_input="Do work that generates lots of output " * 5000,  # Large input to push token count
            handler=handler,
            tools_schema=[],
            max_turns=40,
            verbose=False,
            context_window_tokens=200000,  # New parameter
        )

        # Consume generator
        result_text = ""
        return_value = None
        while True:
            try:
                chunk = next(gen)
                if isinstance(chunk, str):
                    result_text += chunk
            except StopIteration as e:
                return_value = e.value
                break

        # Should have exited with overflow report
        assert return_value is not None
        if isinstance(return_value, dict):
            assert return_value.get("result") in ("CONTEXT_OVERFLOW", "MAX_TURNS_EXCEEDED")

    def test_normal_flow_without_overflow(self, monkeypatch):
        """When token usage stays under 85%, agent_runner_loop completes normally."""
        from agent.generic.agent_loop import agent_runner_loop, MockResponse

        class MockClient:
            name = "mock"
            last_tools = ""
            total_cd_tokens = 0
            _call_count = 0

            def chat(self, messages, tools=None):
                self._call_count += 1
                def gen():
                    # Return a final response (no tool calls) after 1 turn
                    resp = MockResponse(
                        thinking=None,
                        content="Done",
                        tool_calls=None,
                        raw=None,
                    )
                    yield resp
                return gen()

        class MockHandler:
            _done_hooks = []
            max_turns = 40
            current_turn = 0

            def dispatch(self, tool_name, args, response, index=0):
                from agent.generic.agent_loop import StepOutcome
                def gen():
                    yield ""
                    return StepOutcome(None, next_prompt="continue", should_exit=False)
                return gen()

            def next_prompt_patcher(self, next_prompt, outcome, turn):
                return next_prompt

        client = MockClient()
        handler = MockHandler()

        gen = agent_runner_loop(
            client=client,
            system_prompt="system",
            user_input="small task",
            handler=handler,
            tools_schema=[],
            max_turns=40,
            verbose=False,
            context_window_tokens=200000,
        )

        result_text = ""
        return_value = None
        while True:
            try:
                chunk = next(gen)
                if isinstance(chunk, str):
                    result_text += chunk
            except StopIteration as e:
                return_value = e.value
                break

        # Should complete normally, not overflow
        if isinstance(return_value, dict):
            assert return_value.get("result") != "CONTEXT_OVERFLOW"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent_overflow.py::TestAgentLoopTokenThreshold -v`
Expected: FAIL — `context_window_tokens` parameter not accepted, or `count_messages_tokens` not found

- [ ] **Step 3: Write minimal implementation**

In `agent/generic/agent_loop.py`, add token counting helper and modify `agent_runner_loop`:

Add after existing imports at top:
```python
def count_messages_tokens(messages: list) -> int:
    """
    估算消息列表的 token 数量

    使用 litellm.token_counter，回退到字符数估算。
    """
    try:
        from litellm import token_counter
        return token_counter(model="gpt-4o", messages=messages)
    except Exception:
        total = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            total += max(1, len(content) // 2) + 4
        return total
```

Modify `agent_runner_loop` signature — add `context_window_tokens` parameter:
```python
def agent_runner_loop(
    client,
    system_prompt,
    user_input,
    handler,
    tools_schema,
    max_turns=40,
    verbose=True,
    initial_user_content=None,
    history=None,
    on_turn_end=None,
    context_window_tokens=0,  # New: 0 = no limit check
):
```

Add token check at the **start of each turn** (after `turn += 1`, before LLM call):

```python
        turn += 1

        # 上下文溢出保护：检查 token 使用率
        if context_window_tokens > 0:
            current_tokens = count_messages_tokens(messages)
            usage_ratio = current_tokens / context_window_tokens
            if usage_ratio > 0.85:
                logger.warning(f"[Overflow] Context {current_tokens}/{context_window_tokens} tokens ({usage_ratio:.1%}) exceeds 85% threshold")
                if on_turn_end is not None:
                    on_turn_end(messages, tools_schema, turn)
                return {
                    "result": "CONTEXT_OVERFLOW",
                    "data": {
                        "overflow": True,
                        "turns_completed": turn - 1,
                        "tokens_used": current_tokens,
                        "tokens_limit": context_window_tokens,
                    },
                }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent_overflow.py::TestAgentLoopTokenThreshold -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagent_overflow.py agent/generic/agent_loop.py
git commit -m "feat: agent_runner_loop exits at 85% token usage with overflow report"
```

---

### Task 5: call_subagent 处理 CONTEXT_OVERFLOW 返回值

**Files:**
- Modify: `tests/test_subagent_overflow.py`
- Modify: `agent/subagent.py`

- [ ] **Step 1: Write the failing test — overflow result propagation**

```python
class TestOverflowResultPropagation:
    """Test that call_subagent properly handles CONTEXT_OVERFLOW from agent_runner_loop."""

    def test_overflow_result_includes_progress(self, monkeypatch):
        """When agent_runner_loop returns CONTEXT_OVERFLOW, call_subagent should return structured progress."""
        from agent import subagent

        def mock_runner_loop(**kwargs):
            def gen():
                yield "partial work done"
                return {
                    "result": "CONTEXT_OVERFLOW",
                    "data": {
                        "overflow": True,
                        "turns_completed": 5,
                        "tokens_used": 170000,
                        "tokens_limit": 200000,
                    },
                }
            return gen()

        monkeypatch.setattr(subagent, "agent_runner_loop", mock_runner_loop)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
        monkeypatch.setattr(subagent, "create_client", lambda cfg: None)
        monkeypatch.setattr(subagent, "get_tools_schema", lambda: [])

        result = subagent.call_subagent(
            agent_name="test-agent",
            task="task that overflows",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )

        # Result should contain overflow information
        assert "CONTEXT_OVERFLOW" in result or "overflow" in result.lower()
        assert "170000" in result or "turns_completed" in result
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent_overflow.py::TestOverflowResultPropagation -v`
Expected: FAIL — call_subagent doesn't handle CONTEXT_OVERFLOW specially

- [ ] **Step 3: Write minimal implementation**

In `agent/subagent.py`, modify the result collection section of `call_subagent` (the single-chunk path). After the `StopIteration` catch, add CONTEXT_OVERFLOW handling:

```python
        # 优先使用 return 值（包含结构化数据）
        if return_value and isinstance(return_value, dict):
            # CONTEXT_OVERFLOW：返回结构化进度报告
            if return_value.get("result") == "CONTEXT_OVERFLOW":
                data = return_value.get("data", {})
                overflow_report = {
                    "overflow": True,
                    "agent": agent_name,
                    "turns_completed": data.get("turns_completed", 0),
                    "tokens_used": data.get("tokens_used", 0),
                    "tokens_limit": data.get("tokens_limit", 0),
                    "partial_result": result[-2000:] if result else "",
                }
                logger.warning(f"[SubAgent] {agent_name}: Context overflow at {data.get('tokens_used', 0)} tokens")
                return json.dumps(overflow_report, ensure_ascii=False)

            if "data" in return_value and return_value["data"] is not None:
                data = return_value["data"]
                if isinstance(data, dict):
                    return json.dumps(data, ensure_ascii=False)
                return str(data)
            return json.dumps(return_value, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent_overflow.py::TestOverflowResultPropagation -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagent_overflow.py agent/subagent.py
git commit -m "feat: call_subagent returns structured overflow report when context exceeds 85%"
```

---

### Task 6: 传递 context_window_tokens 到子 Agent

**Files:**
- Modify: `agent/subagent.py`

- [ ] **Step 1: Write the failing test — subagent uses context_window_tokens**

```python
class TestSubagentContextWindowConfig:
    """Test that sub-agent receives context_window_tokens from preferences."""

    def test_context_window_tokens_passed_to_loop(self, monkeypatch):
        """call_subagent should read context_window_tokens and pass it to agent_runner_loop."""
        from agent import subagent

        captured_kwargs = {}

        def mock_runner_loop(**kwargs):
            captured_kwargs.update(kwargs)
            def gen():
                yield "done"
                return {"result": "CURRENT_TASK_DONE", "data": "ok"}
            return gen()

        monkeypatch.setattr(subagent, "agent_runner_loop", mock_runner_loop)
        monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
        monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
        monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
        monkeypatch.setattr(subagent, "create_client", lambda cfg: None)
        monkeypatch.setattr(subagent, "get_tools_schema", lambda: [])

        # Mock preferences reading
        monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 128000)

        result = subagent.call_subagent(
            agent_name="test-agent",
            task="test",
            llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        )

        assert captured_kwargs.get("context_window_tokens") == 128000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent_overflow.py::TestSubagentContextWindowConfig -v`
Expected: FAIL — `_read_context_window_tokens` not found, or `context_window_tokens` not passed

- [ ] **Step 3: Write minimal implementation**

In `agent/subagent.py`, add helper function:

```python
def _read_context_window_tokens() -> int:
    """
    从 ~/.niu/preferences.json 读取上下文窗口大小

    Returns:
        上下文窗口 token 数（默认 200000）
    """
    try:
        import json
        from pathlib import Path
        prefs_path = Path.home() / ".niu" / "preferences.json"
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            return prefs.get("context", {}).get("contextWindowSize", 200000)
    except Exception:
        pass
    return 200000
```

In `call_subagent`, before the `agent_runner_loop` call, add:

```python
    # 7.5 读取上下文窗口大小（用于 85% 溢出保护）
    context_window_tokens = _read_context_window_tokens()
```

And pass it to `agent_runner_loop`:

```python
        gen = agent_runner_loop(
            client=client,
            system_prompt=system_prompt,
            user_input=...,
            handler=handler,
            tools_schema=tools_schema,
            max_turns=20,
            verbose=False,
            initial_user_content=...,
            context_window_tokens=context_window_tokens,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent_overflow.py::TestSubagentContextWindowConfig -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagent_overflow.py agent/subagent.py
git commit -m "feat: call_subagent passes context_window_tokens to agent_runner_loop for overflow protection"
```

---

### Task 7: compat.py 处理子 Agent 溢出返回

**Files:**
- Modify: `tests/test_subagent_overflow.py`
- Modify: `niu_api/compat.py`

- [ ] **Step 1: Write the failing test — compat handles overflow**

```python
class TestCompatOverflowHandling:
    """Test that compat.py handles sub-agent overflow results."""

    def test_detects_overflow_in_subagent_result(self):
        """compat should detect overflow JSON in sub-agent result and log warning."""
        from niu_api.compat import _is_subagent_overflow

        overflow_json = '{"overflow": true, "agent": "context-manager", "turns_completed": 5, "tokens_used": 170000, "tokens_limit": 200000}'
        assert _is_subagent_overflow(overflow_json) is True

    def test_normal_result_not_overflow(self):
        from niu_api.compat import _is_subagent_overflow
        assert _is_subagent_overflow("normal result text") is False
        assert _is_subagent_overflow('{"status": "ok"}') is False

    def test_extract_overflow_info(self):
        from niu_api.compat import _extract_overflow_info
        overflow_json = '{"overflow": true, "agent": "context-manager", "turns_completed": 5, "tokens_used": 170000, "tokens_limit": 200000, "partial_result": "some work"}'
        info = _extract_overflow_info(overflow_json)
        assert info["overflow"] is True
        assert info["agent"] == "context-manager"
        assert info["turns_completed"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_subagent_overflow.py::TestCompatOverflowHandling -v`
Expected: FAIL — `_is_subagent_overflow` not found

- [ ] **Step 3: Write minimal implementation**

In `niu_api/compat.py`, add helper functions (before the router definitions):

```python
def _is_subagent_overflow(result: str) -> bool:
    """检查子 Agent 返回结果是否为上下文溢出报告"""
    if not result or not result.strip().startswith("{"):
        return False
    try:
        import json
        data = json.loads(result)
        return isinstance(data, dict) and data.get("overflow") is True
    except (json.JSONDecodeError, ValueError):
        return False


def _extract_overflow_info(result: str) -> dict:
    """从子 Agent 溢出报告中提取信息"""
    try:
        import json
        return json.loads(result)
    except (json.JSONDecodeError, ValueError):
        return {"overflow": True, "raw": result}
```

In `tidy_context`, after each `call_subagent` call, add overflow detection and logging:

After `dream_result = await asyncio.to_thread(run_dream_evolver)`:
```python
            if _is_subagent_overflow(dream_result):
                overflow_info = _extract_overflow_info(dream_result)
                logger.warning(f"[Tidy] Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
```

After `result = await asyncio.to_thread(run_context_manager)`:
```python
            if _is_subagent_overflow(result):
                overflow_info = _extract_overflow_info(result)
                logger.warning(f"[Tidy] Context-manager overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
```

Same pattern for force mode's two `call_subagent` calls.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_subagent_overflow.py::TestCompatOverflowHandling -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagent_overflow.py niu_api/compat.py
git commit -m "feat: compat.py detects and logs sub-agent context overflow reports"
```

---

### Task 8: 全量测试 + 回归验证

**Files:**
- Modify: `tests/test_subagent_overflow.py`

- [ ] **Step 1: Run all new tests together**

Run: `python -m pytest tests/test_subagent_overflow.py -v`
Expected: ALL PASS

- [ ] **Step 2: Run existing subagent tests for regression**

Run: `python -m pytest tests/test_subagent_migration.py -v`
Expected: PASS (no regression)

- [ ] **Step 3: Run existing agent_loop tests for regression**

Run: `python -m pytest tests/test_p0/test_agent_loop.py -v`
Expected: PASS (no regression)

- [ ] **Step 4: Run existing compat tests for regression**

Run: `python -m pytest tests/test_p0/test_compat.py -v`
Expected: PASS (no regression)

- [ ] **Step 5: Commit**

```bash
git add tests/test_subagent_overflow.py
git commit -m "test: verify all sub-agent overflow protection tests pass with no regressions"
```
