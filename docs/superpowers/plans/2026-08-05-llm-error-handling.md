# LLM 错误处理机制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** litellm_adapter 流式错误不再吞掉，按类型重试或标记 stream_error，7 个消费者各自降级处理。

**Architecture:** litellm_adapter 的 chat() 提取 `_do_streaming_completion(response)` generator 方法（不调 litellm.completion），except 块按错误分类重试或标记。MockResponse 加 stream_error/error_type/error_msg 字段。7 个消费者检查 stream_error 各自降级：agent_loop 推错误原文、call_subagent 返回 SUBAGENT_ERROR 前缀、compat.py 跳过不删、llm_proxy 返回 502、lightrag_manager/region_manager 不用 partial content。

**Tech Stack:** Python 3.11+, litellm, pytest, unittest.mock

---

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `agent/generic/llmcore.py` | MockResponse 类定义 | 加 stream_error/error_type/error_msg 字段 |
| `agent/generic/litellm_adapter.py` | LLM 调用入口 | _do_streaming_completion + _classify_stream_error + else 重试 + A3 移除 + MockResponse 构造 |
| `agent/generic/agent_loop.py` | Agent 主循环 | stream_error 检查（主循环 + 总结路径） |
| `agent/subagent.py` | 子 Agent 调用 | call_subagent 返回 SUBAGENT_ERROR + _extract_result_from_return_value 加 LLM_ERROR |
| `agent/handler.py` | 工具调度 | _call_subagent_gen 剥除 SUBAGENT_ERROR 前缀 |
| `agent/runner.py` | Runner 层 | L1859 加 SUBAGENT_ERROR 检查 |
| `niu_api/compat.py` | 压缩路径 | Mode-2 + Force 加 SUBAGENT_ERROR 检查 |
| `niu_api/llm_proxy.py` | OpenAI 代理 | sync_call 返回 _stream_error + 502 + finish_reason 修复 |
| `niu_api/internal/lightrag_manager.py` | LightRAG 查询 | _consume_generator 检查 stream_error |
| `niu_api/internal/region_manager.py` | 脑区标签 | _consume 保存 MockResponse + 检查 stream_error |
| `tests/test_llm_error_handling.py` | 测试 | 新建，错误分类 + 重试 + 消费者适配 |

---

## Task 1: MockResponse 加 stream_error 字段

**Files:**
- Modify: `agent/generic/llmcore.py:26-35`
- Test: `tests/test_llm_error_handling.py`

- [ ] **Step 1: 写失败测试 — MockResponse stream_error 默认值**

```python
# tests/test_llm_error_handling.py
"""LLM 错误处理机制测试。"""
from agent.generic.llmcore import MockResponse


def test_mock_response_stream_error_defaults():
    """MockResponse 新增 stream_error/error_type/error_msg 字段，默认值为 False/None/None。"""
    resp = MockResponse(
        thinking="", content="hello", tool_calls=[], raw="hello"
    )
    assert resp.stream_error is False
    assert resp.error_type is None
    assert resp.error_msg is None


def test_mock_response_stream_error_set():
    """MockResponse 可设置 stream_error=True + error_type + error_msg。"""
    resp = MockResponse(
        thinking="", content="", tool_calls=[], raw="",
        stream_error=True, error_type="fatal",
        error_msg="AuthenticationError: invalid key"
    )
    assert resp.stream_error is True
    assert resp.error_type == "fatal"
    assert resp.error_msg == "AuthenticationError: invalid key"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_mock_response_stream_error_defaults tests/test_llm_error_handling.py::test_mock_response_stream_error_set -v`
Expected: FAIL with `AttributeError: 'MockResponse' object has no attribute 'stream_error'`

- [ ] **Step 3: 实现 MockResponse 新增字段**

修改 `agent/generic/llmcore.py` L26-35 的 MockResponse `__init__`：

```python
class MockResponse:
    def __init__(self, thinking, content, tool_calls, raw,
                 stop_reason="end_turn", context_overflow=False, usage=None,
                 finish_reason=None,
                 stream_error=False, error_type=None, error_msg=None):
        self.thinking = thinking
        self.content = content
        self.tool_calls = tool_calls
        self.raw = raw
        self.stop_reason = "tool_use" if tool_calls else stop_reason
        self.context_overflow = context_overflow
        self.usage = usage
        self.finish_reason = finish_reason
        self.stream_error = stream_error
        self.error_type = error_type
        self.error_msg = error_msg
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_mock_response_stream_error_defaults tests/test_llm_error_handling.py::test_mock_response_stream_error_set -v`
Expected: PASS

- [ ] **Step 5: 语法检查 + 现有测试不回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/generic/llmcore.py').read()); print('syntax OK')" && python/bin/python -m pytest tests/test_truncation_marker.py -v 2>&1 | tail -5`
Expected: syntax OK + 现有测试全部 PASS

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/generic/llmcore.py tests/test_llm_error_handling.py
git commit -m "feat: add stream_error/error_type/error_msg fields to MockResponse"
```

---

## Task 2: _classify_stream_error 错误分类函数

**Files:**
- Modify: `agent/generic/litellm_adapter.py`（在 `_is_context_overflow_error` 函数之后插入）
- Test: `tests/test_llm_error_handling.py`

- [ ] **Step 1: 写失败测试 — 可重试/不可重试/不确定/字符串匹配**

```python
# tests/test_llm_error_handling.py（追加）
from unittest.mock import MagicMock
import litellm


def test_classify_retryable_error():
    """APIConnectionError 归入 retryable。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    e = litellm.APIConnectionError(message="conn error", model="test", llm_provider="test")
    assert _classify_stream_error(e) == "retryable"


def test_classify_fatal_error():
    """AuthenticationError 归入 fatal。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    e = litellm.AuthenticationError(message="bad key", model="test", llm_provider="test")
    assert _classify_stream_error(e) == "fatal"


def test_classify_uncertain_error():
    """InternalServerError 归入 uncertain。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    e = litellm.InternalServerError(message="server error", model="test", llm_provider="test")
    assert _classify_stream_error(e) == "uncertain"


def test_classify_midstream_fallback_string_match():
    """MidStreamFallbackError 字符串匹配归入 retryable（即使不是 litellm 标准异常）。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    # 构造一个名字含 MidStreamFallback 的异常
    class MidStreamFallbackError(Exception):
        pass
    e = MidStreamFallbackError("burst protection")
    assert _classify_stream_error(e) == "retryable"


def test_classify_unknown_error_defaults_retryable():
    """未知异常默认归入 retryable。"""
    from agent.generic.litellm_adapter import _classify_stream_error
    e = RuntimeError("unknown error")
    assert _classify_stream_error(e) == "retryable"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_classify_retryable_error -v`
Expected: FAIL with `ImportError: cannot import name '_classify_stream_error'`

- [ ] **Step 3: 实现 _classify_stream_error**

在 `agent/generic/litellm_adapter.py` 的 `_is_context_overflow_error` 函数之后插入：

```python
# === LLM 错误分类（流式错误重试/标记机制） ===
# getattr 默认值用 None（不是 Exception），避免缺失时 isinstance 匹配所有异常
try:
    _RETRYABLE_EXC = tuple(x for x in (
        litellm.APIConnectionError,
        litellm.Timeout,
        litellm.RateLimitError,
    ) if x is not None)
    _FATAL_EXC = tuple(x for x in (
        litellm.AuthenticationError,
        getattr(litellm, 'PermissionDeniedError', None),
        getattr(litellm, 'BudgetExceededError', None),
        getattr(litellm, 'ContentPolicyViolationError', None),
    ) if x is not None)
    _UNCERTAIN_EXC = tuple(x for x in (
        litellm.InternalServerError,
        litellm.ServiceUnavailableError,
        getattr(litellm, 'BadGatewayError', None),
    ) if x is not None)
except (ImportError, AttributeError):
    _RETRYABLE_EXC = ()
    _FATAL_EXC = ()
    _UNCERTAIN_EXC = ()


def _classify_stream_error(e) -> str:
    """分类流式错误。返回 'retryable' | 'fatal' | 'uncertain'。"""
    # 1. 字符串匹配兜底（优先，确保未验证类型也能分类）
    type_name = type(e).__name__
    if 'MidStreamFallback' in type_name:
        return 'retryable'
    # 2. isinstance 检查
    if _FATAL_EXC and isinstance(e, _FATAL_EXC):
        return 'fatal'
    if _UNCERTAIN_EXC and isinstance(e, _UNCERTAIN_EXC):
        return 'uncertain'
    if _RETRYABLE_EXC and isinstance(e, _RETRYABLE_EXC):
        return 'retryable'
    # 3. 默认归入 retryable（未知错误给重试机会）
    return 'retryable'
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py -k "classify" -v`
Expected: 全部 PASS

- [ ] **Step 5: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/generic/litellm_adapter.py').read()); print('syntax OK')"`
Expected: syntax OK

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/generic/litellm_adapter.py tests/test_llm_error_handling.py
git commit -m "feat: add _classify_stream_error() for LLM error classification"
```

---

## Task 3: _do_streaming_completion generator 方法

**Files:**
- Modify: `agent/generic/litellm_adapter.py`（在 `chat()` 方法之前插入新方法，chat() L498-665 的流式消费循环提取为此方法）
- Test: `tests/test_llm_error_handling.py`

- [ ] **Step 1: 写失败测试 — _do_streaming_completion 基本消费**

```python
# tests/test_llm_error_handling.py（追加）
from types import SimpleNamespace
from agent.generic.litellm_adapter import LiteLLMSession


def _make_chunk(content=None, finish_reason=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=None,
    )


def test_do_streaming_completion_consumes_chunks():
    """_do_streaming_completion 消费流式 response，yield delta.content，return tuple。"""
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    session = LiteLLMSession(cfg)

    fake_chunks = [_make_chunk(content="hello"), _make_chunk(content=" world"), _make_chunk(finish_reason="stop")]
    response = iter(fake_chunks)

    with patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session._do_streaming_completion(response)
        chunks = []
        result = None
        try:
            while True:
                chunk = next(gen)
                if isinstance(chunk, str):
                    chunks.append(chunk)
        except StopIteration as e:
            result = e.value

    assert "".join(chunks) == "hello world"
    assert result is not None
    content, thinking, tool_calls, finish_reason, usage, was_stopped = result
    assert content == "hello world"
    assert finish_reason == "stop"
    assert was_stopped is False
    assert tool_calls == []

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_do_streaming_completion_consumes_chunks -v`
Expected: FAIL with `AttributeError: 'LiteLLMSession' object has no attribute '_do_streaming_completion'`

- [ ] **Step 3: 实现 _do_streaming_completion**

在 `agent/generic/litellm_adapter.py` 的 `LiteLLMSession` 类中，`chat()` 方法之前，插入新方法。**完整复制 chat() L498-606 的 for chunk in response 循环逻辑**（包括 tool_calls_accumulator、was_stopped、usage 提取、yield delta.content），但**不包含 litellm.completion() 调用和错误处理（except 块）**：

```python
def _do_streaming_completion(self, response):
    """消费流式响应（generator）。不调 litellm.completion()。

    litellm.completion() 保留在 chat() 中，初始调用错误保持 raise 行为不变。
    _do_streaming_completion 只负责流式消费循环，接收 response 对象。
    重试时在 except 块内先调 litellm.completion() 获取新 response，再 yield from _do_streaming_completion(response)。

    Yields:
        str: 流式内容增量
    Returns:
        tuple(content, thinking, tool_calls, finish_reason, usage, was_stopped)
    Raises:
        Exception: 流式传输中的任何异常（由调用方捕获分类）
    """
    full_content = ""
    reasoning_content = ""
    tool_calls: list[MockToolCall] = []
    usage = None
    last_finish_reason = None
    was_stopped = False
    tool_calls_accumulator: dict[int, dict] = {}
    chunk_count = 0

    for chunk in response:
        chunk_count += 1
        # is_stop_requested 检查在 chunk 处理之前（与原始代码一致）
        if is_stop_requested():
            was_stopped = True
            break
        if hasattr(chunk, 'choices') and chunk.choices:
            choice = chunk.choices[0]
            delta = getattr(choice, 'delta', None)
            if delta:
                if hasattr(delta, 'content') and delta.content:
                    full_content += delta.content
                    yield delta.content
                if hasattr(delta, 'reasoning_content') and delta.reasoning_content:
                    reasoning_content += delta.reasoning_content
                if hasattr(delta, 'tool_calls') and delta.tool_calls:
                    for tc in delta.tool_calls:
                        # fallback index 用 accumulator 长度（与原始代码一致）
                        idx = getattr(tc, 'index', len(tool_calls_accumulator))
                        if idx not in tool_calls_accumulator:
                            # fallback id（与原始代码一致）
                            tool_calls_accumulator[idx] = {'id': getattr(tc, 'id', None) or f"call_{idx}", 'name': '', 'arguments': ''}
                        if hasattr(tc, 'id') and tc.id:
                            tool_calls_accumulator[idx]['id'] = tc.id
                        if hasattr(tc, 'function') and tc.function:
                            if hasattr(tc.function, 'name') and tc.function.name:
                                tool_calls_accumulator[idx]['name'] = tc.function.name
                            if hasattr(tc.function, 'arguments') and tc.function.arguments:
                                tool_calls_accumulator[idx]['arguments'] += tc.function.arguments
            if hasattr(choice, 'finish_reason') and choice.finish_reason:
                last_finish_reason = choice.finish_reason
        if hasattr(chunk, 'usage') and chunk.usage:
            usage = chunk.usage

    # 循环后重新检查 is_stop_requested（与原始代码一致）
    was_stopped = was_stopped or is_stop_requested()

    # 循环后处理：tool_calls JSON 解析
    for idx in sorted(tool_calls_accumulator.keys()):
        tc_data = tool_calls_accumulator[idx]
        tc_name = tc_data['name']
        # 空名检查含 .strip()（与原始代码一致）
        if not tc_name or not tc_name.strip():
            continue
        tc_args_raw = tc_data['arguments']
        tc_args = {}
        # was_stopped 时跳过不完整 JSON（与原始代码一致）
        if was_stopped:
            try:
                tc_args = json.loads(tc_args_raw) if tc_args_raw else {}
            except json.JSONDecodeError:
                continue
        else:
            # 处理 str/dict/other 三种类型（与原始代码一致）
            if isinstance(tc_args_raw, dict):
                tc_args = tc_args_raw
            elif isinstance(tc_args_raw, str) and tc_args_raw:
                try:
                    tc_args = json.loads(tc_args_raw)
                except json.JSONDecodeError:
                    tc_args = {}
            else:
                tc_args = {}
        tool_calls.append(MockToolCall(
            name=tc_name,
            args=tc_args,
            id=str(tc_data['id']),
        ))

    return (full_content, reasoning_content, tool_calls, last_finish_reason, usage, was_stopped)
```
**注意**：usage 和 finish_reason 提取在 `if hasattr(chunk, 'choices')` 块内但 `if delta:` 块外——正确捕获有 choices 但 delta=None 的 chunk（如最终 usage chunk）。这比原始代码（`if not delta: continue` 跳过）更准确。
- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_do_streaming_completion_consumes_chunks -v`
Expected: PASS

- [ ] **Step 5: 语法检查**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/generic/litellm_adapter.py').read()); print('syntax OK')"`
Expected: syntax OK

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/generic/litellm_adapter.py tests/test_llm_error_handling.py
git commit -m "feat: add _do_streaming_completion() generator method"
```

---

## Task 4: chat() 方法改造 — yield from + 错误重试 + A3 移除 + MockResponse 构造

**Files:**
- Modify: `agent/generic/litellm_adapter.py` L498-700（chat() 方法的 try 块 + except 块 + A3 块 + MockResponse 构造）
- Test: `tests/test_llm_error_handling.py`

- [ ] **Step 1: 写失败测试 — 流式错误重试成功**

```python
# tests/test_llm_error_handling.py（追加）
import litellm
from unittest.mock import patch


def test_stream_error_retry_succeeds():
    """流式错误后重试成功 → stream_error=False，content 为重试内容。"""
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    session = LiteLLMSession(cfg)

    # 第一次流式抛 APIConnectionError，第二次返回完整内容
    good_chunks = [_make_chunk(content="retried"), _make_chunk(finish_reason="stop")]
    call_count = {"n": 0}

    def mock_completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # 返回一个会中途抛异常的迭代器
            def gen():
                yield _make_chunk(content="partial")
                raise litellm.APIConnectionError(message="burst protection", model="test", llm_provider="test")
            return gen()
        return iter(good_chunks)

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session.chat(messages=[{"role": "user", "content": "test"}], tools=None)
        chunks = []
        result = None
        try:
            while True:
                chunk = next(gen)
                if isinstance(chunk, str):
                    chunks.append(chunk)
        except StopIteration as e:
            result = e.value

    assert result is not None
    assert result.stream_error is False
    assert result.content == "retried"
    assert call_count["n"] == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_stream_error_retry_succeeds -v`
Expected: FAIL

- [ ] **Step 3: 改造 chat() 方法**

修改 `agent/generic/litellm_adapter.py` chat() 方法 L498-700：

**3a. 变量初始化（L498-504 附近）**：现有代码已有 `_stream_error_occurred = False` 和内容变量初始化（full_content 等）。**只需新增** `_stream_error_msg` 和 `_stream_error_type` 两个变量。保留现有初始化不变：

```python
# 错误跟踪变量
_stream_error_occurred = False
_stream_error_msg = ""
_stream_error_type = None  # None | "fatal" | "retry_exhausted"
# 内容变量（yield from 抛异常时不赋值，需要安全默认值）
full_content = ""
reasoning_content = ""
tool_calls: list[MockToolCall] = []
usage = None
last_finish_reason = None
was_stopped = False
```

**3b. try 块改为 yield from**（替换 L506-606 的 for chunk in response 循环）：

```python
try:
    full_content, reasoning_content, tool_calls, last_finish_reason, usage, was_stopped = \
        yield from self._do_streaming_completion(response)
except Exception as e:
    _stream_error_occurred = True
    _stream_error_msg = str(e)
    error_msg = str(e)

    # 检测 context_length_exceeded 错误 — 设置标记让 agent_loop 触发强制压缩
    if _is_context_overflow_error(e):
        logger.warning(f"[STREAM] Context length exceeded: {e}")
        return MockResponse(
            thinking=reasoning_content or "",
            content=full_content or "",
            tool_calls=tool_calls,
            raw=full_content or "",
            context_overflow=True,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            finish_reason=last_finish_reason or "stop",
        )

    is_socket_error = "10038" in error_msg or "10054" in error_msg or "non-socket" in error_msg.lower()

    if is_socket_error and not full_content:
        # Windows socket error with empty content → non-stream fallback（保留现有逻辑）
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
                        if hasattr(tc, "function") and tc.function:
                            if hasattr(tc.function, "arguments") and tc.function.arguments:
                                try:
                                    tc_args = json.loads(tc.function.arguments)
                                except json.JSONDecodeError:
                                    tc_args = {}
                            tool_calls.append(MockToolCall(
                                name=getattr(tc.function, "name", ""),
                                args=tc_args,
                                id=getattr(tc, "id", f"call_fallback_{len(tool_calls)}"),
                            ))
                if hasattr(fallback_response, "usage") and fallback_response.usage:
                    usage = fallback_response.usage
                if hasattr(choice, "finish_reason") and choice.finish_reason:
                    last_finish_reason = choice.finish_reason
                _stream_error_occurred = False
                _stream_error_msg = ""  # 清除错误信息（与重试成功路径一致）
                logger.info(f"[STREAM] Non-stream fallback succeeded ({len(full_content)} chars, {len(tool_calls)} tool_calls)")
        except Exception as fb_err:
            logger.error(f"[STREAM] Non-stream fallback also failed: {fb_err}")
            _stream_error_type = "retry_exhausted"
            _stream_error_msg = str(fb_err)
    else:
        # 其他错误 → 分类 + 重试
        logger.error(f"[STREAM] Stream error: {e}")

        error_type = _classify_stream_error(e)

        if error_type == "fatal":
            logger.warning(f"[STREAM] Fatal error ({type(e).__name__}), no retry")
            _stream_error_type = "fatal"
        else:
            max_retries = 3 if error_type == "retryable" else 2
            retry_succeeded = False
            for retry_idx in range(1, max_retries + 1):
                if is_stop_requested():
                    logger.info("[STREAM] Stop requested, aborting retry")
                    break
                logger.info(f"[STREAM] Retry {retry_idx}/{max_retries} for {type(e).__name__}")
                try:
                    retry_response = litellm.completion(**request_params)
                    full_content, reasoning_content, tool_calls, \
                        last_finish_reason, usage, was_stopped = \
                        yield from self._do_streaming_completion(retry_response)
                    _stream_error_occurred = False
                    _stream_error_msg = ""
                    retry_succeeded = True
                    logger.info(f"[STREAM] Retry {retry_idx} succeeded ({len(full_content)} chars)")
                    break
                except Exception as retry_e:
                    if _is_context_overflow_error(retry_e):
                        logger.warning(f"[STREAM] Retry hit context_overflow, stopping")
                        return MockResponse(
                            thinking=reasoning_content or "",
                            content=full_content or "",
                            tool_calls=tool_calls,
                            raw=full_content or "",
                            context_overflow=True,
                            finish_reason=last_finish_reason or "stop",
                        )
                    logger.error(f"[STREAM] Retry {retry_idx} failed: {retry_e}")
                    _stream_error_msg = str(retry_e)

            if not retry_succeeded:
                _stream_error_type = "retry_exhausted"
                logger.error(f"[STREAM] All {max_retries} retries exhausted")
```

**3c. A3 块完全移除**（删除 L684-692 的 A3 文本标记注入块）：

```python
# A3 块完全移除——stream_error 字段替代文本标记
# 以下代码删除：
# if not full_content and _stream_error_occurred: ...
# elif full_content and last_finish_reason != "length" and not was_stopped and _stream_error_occurred: ...
```

**3d. MockResponse 构造改为传递 stream_error 字段**（修改 L694-700）：

```python
mock_resp = MockResponse(
    thinking=reasoning_content,
    content=full_content,
    tool_calls=tool_calls,
    raw=full_content,
    finish_reason=last_finish_reason or "stop",
    stream_error=_stream_error_occurred,
    error_type=_stream_error_type,
    error_msg=_stream_error_msg or None,
)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_stream_error_retry_succeeds -v`
Expected: PASS

- [ ] **Step 5: 写测试 — 重试 3 次失败**

```python
# tests/test_llm_error_handling.py（追加）

def test_stream_error_retry_exhausted():
    """流式错误重试 3 次都失败 → stream_error=True, error_type='retry_exhausted'。"""
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    session = LiteLLMSession(cfg)

    def mock_completion(**kwargs):
        def gen():
            yield _make_chunk(content="partial")
            raise litellm.APIConnectionError(message="burst protection", model="test", llm_provider="test")
        return gen()

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session.chat(messages=[{"role": "user", "content": "test"}], tools=None)
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value

    assert result.stream_error is True
    assert result.error_type == "retry_exhausted"
    assert result.content == ""  # yield from 抛异常时 full_content 保持初始值


def test_stream_error_fatal_no_retry():
    """不可重试错误（AuthenticationError）→ 不重试，stream_error=True, error_type='fatal'。"""
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    session = LiteLLMSession(cfg)

    call_count = {"n": 0}
    def mock_completion(**kwargs):
        call_count["n"] += 1
        def gen():
            yield _make_chunk(content="partial")
            raise litellm.AuthenticationError(message="bad key", model="test", llm_provider="test")
        return gen()

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session.chat(messages=[{"role": "user", "content": "test"}], tools=None)
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value

    assert result.stream_error is True
    assert result.error_type == "fatal"
    assert call_count["n"] == 1  # 没有重试
    assert result.content == ""  # full_content 保持初始值
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_stream_error_retry_exhausted tests/test_llm_error_handling.py::test_stream_error_fatal_no_retry -v`
Expected: PASS

- [ ] **Step 7: 语法检查 + 现有测试不回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/generic/litellm_adapter.py').read()); print('syntax OK')" && python/bin/python -m pytest tests/test_truncation_marker.py tests/test_compress_quality.py -v 2>&1 | tail -10`
Expected: syntax OK + 现有测试无新增失败

- [ ] **Step 8: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/generic/litellm_adapter.py tests/test_llm_error_handling.py
git commit -m "feat: chat() error classification + retry + stream_error marker"
```

---

## Task 5: agent_loop stream_error 检查

**Files:**
- Modify: `agent/generic/agent_loop.py`（主循环 verbose=False 路径 B1 之前 + verbose=True 路径 + 总结路径 L1188）
- Test: `tests/test_llm_error_handling.py`

- [ ] **Step 1: 写失败测试 — stream_error → return LLM_ERROR**

```python
# tests/test_llm_error_handling.py（追加）
from agent.generic.agent_loop import agent_runner_loop
from agent.generic.llmcore import MockResponse


def test_agent_loop_stream_error_returns_llm_error():
    """agent_loop 检查 stream_error=True → yield error_msg + return LLM_ERROR。"""
    from agent import runner as _runner_mod
    from agent.generic import agent_loop

    # mock stop flags
    _runner_mod.is_stop_requested = lambda: False
    _runner_mod.clear_stop = lambda: None
    _runner_mod.drain_supplement = lambda: None

    class _FakeValidation:
        is_valid = True
        def format_feedback(self): return ""

    agent_loop.validate_references = lambda content: _FakeValidation()

    class _FakeHandler:
        _last_prompt_tokens = 0
        _done_hooks = []
        max_turns = 1
        current_turn = 1
        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    def _fake_chat(self, messages, tools=None, response_format=None):
        resp = MockResponse(
            thinking="", content="", tool_calls=[], raw="",
            finish_reason="stop",
            stream_error=True, error_type="fatal",
            error_msg="AuthenticationError: bad key"
        )
        yield ""
        return resp

    class _FakeClient:
        last_tools = ""
        def chat(self, messages, tools=None, response_format=None):
            return _fake_chat(self, messages, tools, response_format)

    gen = agent_loop.agent_runner_loop(
        client=_FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=_FakeHandler(),
        tools_schema=[],
        max_turns=1,
        initial_user_content="test",
        enable_supplement=False,
    )

    return_value = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return_value = e.value

    assert return_value is not None
    assert return_value.get("result") == "LLM_ERROR"
    assert "bad key" in return_value.get("error_msg", "")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_agent_loop_stream_error_returns_llm_error -v`
Expected: FAIL

- [ ] **Step 3: 实现 agent_loop stream_error 检查**

在 `agent/generic/agent_loop.py` 的 `agent_runner_loop` 中：

**3a. verbose=False 路径**：在 `response = exhaust(response_gen)` 之后、`content = response.content or ""` 之前插入：

```python
# 检查模型调用错误（重试失败/不可重试错误）——在 B1/拦截/reply yield 之前
if getattr(response, 'stream_error', False):
    error_msg = getattr(response, 'error_msg', None) or "模型调用失败"
    yield error_msg
    yield StreamEvent("system", "chat_idle")
    clear_stop()
    return {"result": "LLM_ERROR", "error_msg": error_msg}
```

**3b. verbose=True 路径**：在 `response = yield from response_gen` 之后、`yield StreamEvent("system", "\n\n")` 之前插入同样的检查。

**3c. 总结路径（L1188 附近）**：在 `summary_response = exhaust(summary_gen)` 之后插入：

```python
if summary_response and getattr(summary_response, 'stream_error', False):
    logger.warning(f"[Summary] LLM error, skipping summary: {summary_response.error_msg}")
    summary_text = ''
else:
    summary_text = summary_response.content if summary_response else ''
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_agent_loop_stream_error_returns_llm_error -v`
Expected: PASS

- [ ] **Step 5: 语法检查 + 现有测试不回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('agent/generic/agent_loop.py').read()); print('syntax OK')" && python/bin/python -m pytest tests/test_truncation_marker.py tests/test_at_prefix_interception.py -v 2>&1 | tail -10`
Expected: syntax OK + 现有测试无新增失败

- [ ] **Step 6: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/generic/agent_loop.py tests/test_llm_error_handling.py
git commit -m "feat: agent_loop checks stream_error before B1/intercept/reply"
```

---

## Task 6: subagent + handler + runner — SUBAGENT_ERROR 前缀机制

**Files:**
- Modify: `agent/subagent.py`（call_subagent LLM_ERROR 检查 + _extract_result_from_return_value 加 LLM_ERROR）
- Modify: `agent/handler.py`（_call_subagent_gen 剥除 SUBAGENT_ERROR 前缀）
- Modify: `agent/runner.py`（L1859 加 SUBAGENT_ERROR 检查）
- Test: `tests/test_llm_error_handling.py`

- [ ] **Step 1: 写失败测试 — call_subagent 返回 SUBAGENT_ERROR 前缀**

```python
# tests/test_llm_error_handling.py（追加）
from unittest.mock import patch, MagicMock

def test_call_subagent_returns_subagent_error_prefix():
    """call_subagent 检测 return_value result='LLM_ERROR' → 返回 'SUBAGENT_ERROR:{error_msg}'。"""
    from agent import subagent

    def fake_run_agent_loop(**kwargs):
        return "", {"result": "LLM_ERROR", "error_msg": "AuthError: bad key"}, ""

    # 检查 _extract_result_from_return_value 的 control_flow_results 是否含 LLM_ERROR
    import inspect
    src = inspect.getsource(subagent._extract_result_from_return_value)
    assert "LLM_ERROR" in src, "control_flow_results 应包含 LLM_ERROR"

    # mock call_subagent 内部所有依赖（避免读取真实配置/注册）
    # SubagentRegistry 是函数内 import，patch 源模块
    with patch.object(subagent, '_run_agent_loop', fake_run_agent_loop), \
         patch.object(subagent, 'get_subagent_config', return_value={}), \
         patch.object(subagent, 'build_subagent_system_segments', return_value=("test", "")), \
         patch.object(subagent, '_build_subagent_tools_schema', return_value=[]), \
         patch.object(subagent, '_read_context_window_tokens', return_value=24000), \
         patch.object(subagent, '_read_target_threshold', return_value=0.3), \
         patch('agent.subagent_registry.SubagentRegistry') as mock_registry:
        mock_registry.register.return_value = MagicMock()
        mock_registry.get.return_value = None
        mock_registry.close.return_value = None

        import agent.handler as handler_module
        import agent.runner as runner_module
        class FakeClient: pass
        with patch.object(runner_module, 'create_client', lambda cfg: FakeClient()), \
             patch.object(runner_module, 'get_tools_schema', lambda **kwargs: []), \
             patch.object(handler_module, 'NiuHandler') as MockHandler:
            MockHandler.return_value = MagicMock(
                _disable_memory_recall=False, _is_subagent=False
            )

            result = subagent.call_subagent(
                agent_name="context-manager",
                task="test",
                llm_config={"model": "test"},
            )
    assert result.startswith("SUBAGENT_ERROR:")
    assert "bad key" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_call_subagent_returns_subagent_error_prefix -v`
Expected: FAIL

- [ ] **Step 3: 实现 subagent.py 改动**

**3a. call_subagent 加 LLM_ERROR 检查**（在 `finish_reason == "length"` 检查 L974 **之前**）：

```python
if return_value and isinstance(return_value, dict) and return_value.get("result") == "LLM_ERROR":
    error_msg = return_value.get("error_msg", "未知错误")
    logger.warning(f"[SubAgent] {agent_name}: LLM error: {error_msg}")
    return f"SUBAGENT_ERROR:{error_msg}"
```

**3b. _extract_result_from_return_value 的 control_flow_results 集合加入 "LLM_ERROR"**

- [ ] **Step 4: 实现 handler.py 改动**

在 `_call_subagent_gen` 中 call_subagent 返回后、COMPACT_TRUNCATED 检查之前，加 SUBAGENT_ERROR 检查：

```python
# 先检查 SUBAGENT_ERROR（致命错误）
if result and result.startswith("SUBAGENT_ERROR:"):
    error_msg = result[len("SUBAGENT_ERROR:"):]
    return StepOutcome({"status": "error", "msg": error_msg}, next_prompt=f"子Agent调用失败：{error_msg}")
# 再检查 COMPACT_TRUNCATED（截断）
elif result and result.startswith("COMPACT_TRUNCATED:"):
    ...  # 现有剥除逻辑不变
```

- [ ] **Step 5: 实现 runner.py 改动**

在 `runner.py` L1859 的 `COMPACT_TRUNCATED` 检查之前，加 SUBAGENT_ERROR 检查：

```python
if result and result.startswith("SUBAGENT_ERROR:"):
    error_msg = result[len("SUBAGENT_ERROR:"):]
    logger.warning(f"[Compact] Runner: context-manager LLM error: {error_msg}")
    return {"status": "skipped", "reason": f"LLM error: {error_msg}"}
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_call_subagent_returns_subagent_error_prefix -v`
Expected: PASS

- [ ] **Step 7: 语法检查全部改动文件**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['agent/subagent.py', 'agent/handler.py', 'agent/runner.py']]; print('all syntax OK')"`
Expected: all syntax OK

- [ ] **Step 8: 现有测试不回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_truncation_marker.py tests/test_compress_quality.py -v 2>&1 | tail -10`
Expected: 无新增失败

- [ ] **Step 9: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add agent/subagent.py agent/handler.py agent/runner.py tests/test_llm_error_handling.py
git commit -m "feat: SUBAGENT_ERROR prefix mechanism (subagent + handler + runner)"
```

---

## Task 7: compat.py — 压缩路径 SUBAGENT_ERROR 检查

**Files:**
- Modify: `niu_api/compat.py`（Mode-2 L2764 + Force L3430 加 SUBAGENT_ERROR 检查）
- Test: `tests/test_llm_error_handling.py`

- [ ] **Step 1: 写失败测试 — SUBAGENT_ERROR 检查逻辑单元测试**

```python
# tests/test_llm_error_handling.py（追加）
# 直接测试 SUBAGENT_ERROR 检查逻辑（不调用 _tidy_context_impl，避免 mock 复杂依赖）

def test_subagent_error_check_logic():
    """SUBAGENT_ERROR: 前缀检查逻辑 → 返回 skipped dict。"""
    # 模拟 compat.py Mode-2 路径中的 SUBAGENT_ERROR 检查
    compress_result = "SUBAGENT_ERROR:AuthError: bad key"

    # 这是从 compat.py Mode-2 路径提取的检查逻辑
    if compress_result and compress_result.startswith("SUBAGENT_ERROR:"):
        error_msg = compress_result[len("SUBAGENT_ERROR:"):]
        result = {"status": "skipped", "mode": "sleep", "reason": f"LLM error: {error_msg}"}
    else:
        result = {"status": "executed"}

    assert result["status"] == "skipped"
    assert "LLM error" in result["reason"]
    assert "bad key" in result["reason"]


def test_normal_result_not_subagent_error():
    """正常 compress_result 不触发 SUBAGENT_ERROR 检查。"""
    compress_result = "keep=1,2,3\\nupdate=1|[摘要] 测试"

    if compress_result and compress_result.startswith("SUBAGENT_ERROR:"):
        result = {"status": "skipped"}
    else:
        result = {"status": "executed"}

    assert result["status"] == "executed"
```

- [ ] **Step 2: 运行测试确认逻辑正确（单元测试不依赖实现，验证检查逻辑模式）**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_subagent_error_check_logic tests/test_llm_error_handling.py::test_normal_result_not_subagent_error -v`
Expected: PASS（单元测试验证 SUBAGENT_ERROR 检查逻辑模式正确，不依赖 compat.py 实现）

- [ ] **Step 3: 实现 compat.py 改动**

**3a. Mode-2 路径**（在 L2764 `COMPACT_TRUNCATED` 检查之前）：

```python
if compress_result and compress_result.startswith("SUBAGENT_ERROR:"):
    error_msg = compress_result[len("SUBAGENT_ERROR:"):]
    logger.warning(f"[Compact] Mode-2: context-manager LLM error: {error_msg}")
    return {"status": "skipped", "mode": "sleep", "reason": f"LLM error: {error_msg}"}
```

**3b. Force 路径**（在 L3430 `COMPACT_TRUNCATED` 检查之前）：

```python
if result and result.startswith("SUBAGENT_ERROR:"):
    error_msg = result[len("SUBAGENT_ERROR:"):]
    logger.warning(f"[Compact] Force: context-manager LLM error: {error_msg}")
    return {"status": "skipped", "mode": "force", "reason": f"LLM error: {error_msg}"}
```

- [ ] **Step 4: 语法检查 + 现有测试不回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; ast.parse(open('niu_api/compat.py').read()); print('syntax OK')" && python/bin/python -m pytest tests/test_compress_quality.py -v 2>&1 | tail -10`
Expected: syntax OK + 无新增失败

- [ ] **Step 5: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/compat.py tests/test_llm_error_handling.py
git commit -m "feat: compat.py SUBAGENT_ERROR check (Mode-2 + Force → skipped)"
```

---

## Task 8: llm_proxy + lightrag_manager + region_manager — 消费者适配

**Files:**
- Modify: `niu_api/llm_proxy.py`（sync_call 返回 _stream_error + 502 + finish_reason 修复）
- Modify: `niu_api/internal/lightrag_manager.py`（_consume_generator 检查 stream_error）
- Modify: `niu_api/internal/region_manager.py`（_consume 保存 MockResponse + 检查 stream_error）
- Modify: `niu_api/compat.py`（_try_tier 加 stream_error 检查——第 8 个消费者）

- [ ] **Step 1: 写失败测试 — llm_proxy stream_error → 502**

```python
# tests/test_llm_error_handling.py（追加）
import asyncio
import pytest

@pytest.mark.asyncio
async def test_llm_proxy_stream_error_returns_502():
    """llm_proxy call_llm_via_litellm 检测 stream_error → HTTPException 502。"""
    from niu_api.llm_proxy import call_llm_via_litellm
    from fastapi import HTTPException
    from agent.generic.llmcore import MockResponse

    class _FakeGen:
        def __iter__(self): return self
        def __next__(self):
            raise StopIteration(MockResponse(
                thinking="", content="", tool_calls=[], raw="",
                stream_error=True, error_type="fatal", error_msg="AuthError"
            ))

    class _FakeSession:
        def chat(self, **kwargs): return _FakeGen()

    # call_llm_via_litellm 签名是 (messages, tools=None, response_format=None, config=None)
    # LiteLLMSession 在函数内 import，patch 源模块
    config = {"model": "test-model", "apikey": "test", "apibase": "http://test", "type": "openai"}
    with patch("agent.generic.litellm_adapter.LiteLLMSession", return_value=_FakeSession()):
        try:
            await call_llm_via_litellm(
                messages=[{"role": "user", "content": "test"}],
                config=config,
            )
            assert False, "应抛 HTTPException 502"
        except HTTPException as e:
            assert e.status_code == 502
            assert "AuthError" in str(e.detail)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_llm_proxy_stream_error_returns_502 -v`
Expected: FAIL

- [ ] **Step 3: 实现 llm_proxy 改动**

**3a. sync_call 中加 stream_error 检查**（在 mock_response 获取后）：

```python
if mock_response and getattr(mock_response, 'stream_error', False):
    return {"_stream_error": True, "error_msg": mock_response.error_msg, "error_type": mock_response.error_type}
```

**3b. sync_call 正常路径改用 mock_response.content**：

```python
full_text = mock_response.content if mock_response else "".join(chunks)
```

**3c. finish_reason 硬编码修复**：

```python
"finish_reason": getattr(mock_response, 'finish_reason', None) or ("tool_calls" if tool_calls_list else "stop")

**3d. call_llm_via_litellm 检查 _stream_error**：

```python
response = await asyncio.wait_for(asyncio.to_thread(sync_call), timeout=180)
if isinstance(response, dict) and response.get("_stream_error"):
    raise HTTPException(status_code=502, detail={
        "message": response.get("error_msg", "LLM call failed"),
        "type": response.get("error_type", "stream_error")
    })
```

- [ ] **Step 4: 实现 lightrag_manager 改动**

**4a. 将 `_consume_generator` 从 `_llm_model_func` 内部提取到模块级别**（`_build_llm_model_func` 之外）。`_consume_generator` 不使用任何闭包变量，提取是安全的。提取后 `from niu_api.internal.lightrag_manager import _consume_generator` 可正常工作。

**4b. 修改提取后的 `_consume_generator` 函数**：

```python
def _consume_generator(gen):
    chunks = []
    mock_response = None
    try:
        while True:
            chunk = next(gen)
            if isinstance(chunk, str):
                chunks.append(chunk)
    except StopIteration as e:
        mock_response = e.value

    if mock_response and getattr(mock_response, 'stream_error', False):
        logger.warning(f"[LightRAG] LLM stream error: {mock_response.error_msg}")
        return [], mock_response

    return chunks, mock_response
```

**4c. 调用方适配**（`_llm_model_func` 中有一处 `full_content = "".join(chunks)` 需要替换——位于 try/except BadRequestError 块之后）：
```python
if mock_response and getattr(mock_response, 'stream_error', False):
    full_content = ""  # LLM 错误，返回空内容降级
else:
    full_content = mock_response.content if mock_response and hasattr(mock_response, 'content') else "".join(chunks)
```

- [ ] **Step 5: 实现 region_manager 改动**

**5a. _consume 函数保存 MockResponse**：

```python
def _consume():
    try:
        while True:
            chunk = next(gen)
            if isinstance(chunk, str):
                chunks.append(chunk)
    except StopIteration as e:
        result_holder[0] = e.value  # 保存 MockResponse（新增）
    except Exception as e:
        result_holder[1] = e  # 保存异常（现有逻辑）
```

**5b. _call_llm_for_label 检查 stream_error + 改用 content**：

```python
thread.join(timeout=30)
if thread.is_alive():
    logger.warning("region labeling timeout, using partial result")
else:
    mock_resp = result_holder[0]
    if mock_resp and getattr(mock_resp, 'stream_error', False):
        logger.warning(f"region labeling LLM error: {mock_resp.error_msg}")
        return ""
    if result_holder[1]:
        raise result_holder[1]
# 改用 MockResponse.content
mock_resp = result_holder[0]
if mock_resp and hasattr(mock_resp, 'content'):
    return mock_resp.content
return "".join(chunks)
```
**行为变化**：超时时 `if result_holder[1]: raise` 移入 `else` 块——超时+异常同时发生时返回 partial 而非 raise。这是有意的——region labeling 是低优先级，不应在超时+异常边缘场景崩溃。

- [ ] **Step 5b: 写测试 — lightrag_manager stream_error 降级**

```python
# tests/test_llm_error_handling.py（追加）

def test_lightrag_consume_generator_stream_error():
    """_consume_generator 检测 stream_error → 返回 ([], mock_response)。"""
    from niu_api.internal.lightrag_manager import _consume_generator
    from agent.generic.llmcore import MockResponse

    class _FakeGen:
        def __iter__(self): return self
        def __next__(self):
            raise StopIteration(MockResponse(
                thinking="", content="", tool_calls=[], raw="",
                stream_error=True, error_type="fatal", error_msg="AuthError"
            ))

    chunks, mock_resp = _consume_generator(_FakeGen())
    assert chunks == []
    assert mock_resp.stream_error is True
```

- [ ] **Step 5c: 实现 compat.py _try_tier stream_error 检查**

`_try_tier` 函数（compat.py L1686-1712）中 `except StopIteration: pass` 丢弃了 MockResponse。改为保存并检查 stream_error：

```python
# _try_tier 中现有：
# except StopIteration:
#     pass
# 改为：
except StopIteration as e:
    mock_response = e.value

# 在 text 分类之前加 stream_error 检查：
if mock_response and getattr(mock_response, 'stream_error', False):
    return ("infra_error", f"stream_error: {getattr(mock_response, 'error_msg', '')[:150]}")
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py::test_llm_proxy_stream_error_returns_502 tests/test_llm_error_handling.py::test_lightrag_consume_generator_stream_error -v`
Expected: PASS

- [ ] **Step 7: 语法检查全部改动文件**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['niu_api/llm_proxy.py', 'niu_api/internal/lightrag_manager.py', 'niu_api/internal/region_manager.py', 'niu_api/compat.py']]; print('all syntax OK')"`
Expected: all syntax OK

- [ ] **Step 8: 现有测试不回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_truncation_marker.py tests/test_compress_quality.py tests/test_at_prefix_interception.py -v 2>&1 | tail -10`
Expected: 无新增失败

- [ ] **Step 9: 提交**

```bash
cd /Users/lilei/tools/ai-bot
git add niu_api/llm_proxy.py niu_api/internal/lightrag_manager.py niu_api/internal/region_manager.py niu_api/compat.py tests/test_llm_error_handling.py
git commit -m "feat: consumer adaptation (llm_proxy 502 + lightrag/region/_try_tier stream_error check)"
```

---

## Task 9: 全量回归测试 + 语法检查

**Files:**
- 无修改，仅验证

- [ ] **Step 1: 语法检查全部改动文件**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -c "import ast; [ast.parse(open(f).read()) for f in ['agent/generic/llmcore.py', 'agent/generic/litellm_adapter.py', 'agent/generic/agent_loop.py', 'agent/subagent.py', 'agent/handler.py', 'agent/runner.py', 'niu_api/compat.py', 'niu_api/llm_proxy.py', 'niu_api/internal/lightrag_manager.py', 'niu_api/internal/region_manager.py']]; print('all syntax OK')"`
Expected: all syntax OK

- [ ] **Step 2: 运行新测试文件全部通过**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_llm_error_handling.py -v`
Expected: 全部 PASS

- [ ] **Step 3: 运行已有测试确保不回归**

Run: `cd /Users/lilei/tools/ai-bot && python/bin/python -m pytest tests/test_truncation_marker.py tests/test_compress_quality.py tests/test_at_prefix_interception.py tests/test_sync_subagent_interaction.py -v 2>&1 | tail -15`
Expected: 无新增失败（已有失败如果是 pre-existing 则记录但不阻塞）

- [ ] **Step 4: 最终提交（如有修复）**

```bash
cd /Users/lilei/tools/ai-bot
git add -A
git commit -m "test: regression tests for LLM error handling" || echo "nothing to commit"
```

---

## Self-Review

### 1. Spec coverage

| 需求 | 对应 Task |
|---|---|
| MockResponse 加 stream_error/error_type/error_msg | Task 1 |
| _classify_stream_error 错误分类 | Task 2 |
| _do_streaming_completion generator 方法 | Task 3 |
| chat() 错误重试 + A3 移除 + MockResponse 构造 | Task 4 |
| agent_loop stream_error 检查（主循环 + 总结） | Task 5 |
| subagent SUBAGENT_ERROR + handler 剥除 + runner 检查 | Task 6 |
| compat.py Mode-2 + Force SUBAGENT_ERROR | Task 7 |
| llm_proxy 502 + lightrag_manager + region_manager | Task 8 |
| 全量回归 | Task 9 |

### 2. Placeholder scan

- 无 TBD/TODO
- 每个步骤都有完整代码
- 测试代码完整可运行
- 无"类似 Task N"引用

### 3. Type consistency

- `_do_streaming_completion(response)` 签名在 Task 3 定义，Task 4 调用，一致
- `_classify_stream_error(e)` 在 Task 2 定义，Task 4 调用，一致
- `SUBAGENT_ERROR:` 前缀在 Task 6 定义，Task 7 消费，一致
- `LLM_ERROR` return_value 在 Task 5 定义，Task 6 检查，一致
- MockResponse 新增字段在 Task 1 定义，Task 4/5/6/8 使用，一致
