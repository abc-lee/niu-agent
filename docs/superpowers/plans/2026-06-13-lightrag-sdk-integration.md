# LightRAG SDK Integration — TDD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace LightRAG's OpenAI SDK direct call with LiteLLMSession, eliminate model compatibility issues, and clean up unused proxy endpoints.

**Architecture:** `_llm_model_func` calls `LiteLLMSession.chat()` directly via `asyncio.to_thread`, brain region injection moves from proxy layer into `_llm_model_func`, proxy chat_completions/embeddings endpoints deleted.

**Tech Stack:** Python 3.11, LiteLLM, asyncio, FastAPI, pytest

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `agent/generic/litellm_adapter.py` | LiteLLM SDK adapter — add drop_params for response_format | Modify |
| `niu_api/internal/lightrag_manager.py` | LightRAG instance manager — rewrite `_llm_model_func`, delete old code | Modify |
| `niu_api/llm_proxy.py` | LLM proxy — delete chat_completions/embeddings endpoints, delete brain region import/interception | Modify |
| `tests/test_lightrag_manager.py` | LightRAG manager tests — delete proxy_base_url assertions, add `_llm_model_func` tests | Modify |
| `tests/test_llm_proxy.py` | Proxy tests — delete TestChatCompletions and TestEmbeddingsEndpoint | Modify |
| `tests/test_llm_proxy_injection.py` | Proxy injection tests — delete entire file | Delete |

---

### Task 1: litellm_adapter.py — drop_params for response_format

**Files:**
- Modify: `agent/generic/litellm_adapter.py:351-354`
- Test: `tests/test_litellm_adapter_drop_params.py` (create)

**Why first:** This is the smallest, most isolated change. It only adds one conditional line to an existing function. No other code depends on this change being present first — but Task 2 (the main `_llm_model_func` rewrite) needs it for keyword_extraction to work without errors on non-OpenAI models.

- [ ] **Step 1: Write the failing test**

Create `tests/test_litellm_adapter_drop_params.py`:

```python
"""Test that drop_params=True is set when response_format is passed to LiteLLMSession.chat()."""

from unittest.mock import patch, MagicMock


def test_drop_params_set_when_response_format_present():
    """When response_format is provided, drop_params must be True in request_params."""
    from agent.generic.litellm_adapter import LiteLLMSession

    cfg = {
        "api_type": "openai",
        "apikey": "test-key",
        "apibase": "https://api.openai.com/v1",
        "model": "gpt-4o",
    }
    session = LiteLLMSession(cfg=cfg)

    response_format = {"type": "json_schema", "json_schema": {"name": "test", "strict": True, "schema": {}}}

    # Patch litellm.completion to capture request_params
    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        # Make the mock raise to prevent stream consumption
        mock_completion.side_effect = Exception("stop-test")

        try:
            # chat() is a generator — call next() to trigger one iteration
            gen = session.chat(
                messages=[{"role": "user", "content": "test"}],
                response_format=response_format,
            )
            next(gen)
        except Exception:
            pass

        # Verify litellm.completion was called with drop_params=True
        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs.get("drop_params") is True, (
            f"drop_params should be True when response_format is present, got: {call_kwargs.get('drop_params')}"
        )


def test_drop_params_not_set_when_no_response_format():
    """When no response_format and no reasoning_effort, drop_params should NOT be set."""
    from agent.generic.litellm_adapter import LiteLLMSession

    cfg = {
        "api_type": "openai",
        "apikey": "test-key",
        "apibase": "https://api.openai.com/v1",
        "model": "gpt-4o",
    }
    session = LiteLLMSession(cfg=cfg)

    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        mock_completion.side_effect = Exception("stop-test")

        try:
            gen = session.chat(
                messages=[{"role": "user", "content": "test"}],
            )
            next(gen)
        except Exception:
            pass

        call_kwargs = mock_completion.call_args[1]
        assert "drop_params" not in call_kwargs, (
            f"drop_params should NOT be in request_params when no response_format, got: {call_kwargs}"
        )


def test_drop_params_set_when_reasoning_effort_present():
    """When reasoning_effort is present (but no response_format), drop_params should still be True."""
    from agent.generic.litellm_adapter import LiteLLMSession

    cfg = {
        "api_type": "openai",
        "apikey": "test-key",
        "apibase": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "reasoning_effort": "low",
    }
    session = LiteLLMSession(cfg=cfg)

    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        mock_completion.side_effect = Exception("stop-test")

        try:
            gen = session.chat(
                messages=[{"role": "user", "content": "test"}],
            )
            next(gen)
        except Exception:
            pass

        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs.get("drop_params") is True


def test_drop_params_set_when_both_response_format_and_reasoning_effort():
    """When both response_format and reasoning_effort are present, drop_params should be True."""
    from agent.generic.litellm_adapter import LiteLLMSession

    cfg = {
        "api_type": "openai",
        "apikey": "test-key",
        "apibase": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "reasoning_effort": "low",
    }
    session = LiteLLMSession(cfg=cfg)

    response_format = {"type": "json_schema", "json_schema": {"name": "test", "strict": True, "schema": {}}}

    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        mock_completion.side_effect = Exception("stop-test")

        try:
            gen = session.chat(
                messages=[{"role": "user", "content": "test"}],
                response_format=response_format,
            )
            next(gen)
        except Exception:
            pass

        call_kwargs = mock_completion.call_args[1]
        assert call_kwargs.get("drop_params") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python -m pytest tests/test_litellm_adapter_drop_params.py::test_drop_params_set_when_response_format_present -v`
Expected: FAIL — `drop_params` is not yet set when `response_format` is present

- [ ] **Step 3: Write minimal implementation**

In `agent/generic/litellm_adapter.py`, after line 354 (`if provider_params.get("reasoning_effort"): request_params["drop_params"] = True`), add:

```python
        if response_format is not None:
            request_params["drop_params"] = True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python -m pytest tests/test_litellm_adapter_drop_params.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Verify existing tests still pass**

Run: `cd <repo_root> && python -m pytest tests/test_litellm_adapter_drop_params.py -v && python -m pytest agent/tests/ -v -k "litellm" 2>/dev/null || true`
Expected: New tests PASS, existing tests unaffected

- [ ] **Step 6: Commit**

```bash
git add tests/test_litellm_adapter_drop_params.py agent/generic/litellm_adapter.py
git commit -m "feat: set drop_params=True when response_format is passed to LiteLLMSession.chat()"
```

---

### Task 2: lightrag_manager.py — rewrite `_llm_model_func` to call LiteLLMSession directly

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py`
- Test: `tests/test_lightrag_manager.py`

**Why second:** This is the core change — the new `_llm_model_func` that replaces OpenAI SDK with LiteLLMSession. Everything else (deleting proxy endpoints, cleaning up old code) depends on this working first.

**Pre-requisite:** Task 1 must be completed (drop_params for response_format).

- [ ] **Step 1: Write the failing test for `_llm_model_func` — basic text call**

Add to `tests/test_lightrag_manager.py` (after existing tests):

```python
class TestLlmModelFunc:
    """Test the new _llm_model_func that calls LiteLLMSession directly."""

    @pytest.fixture
    def mock_llm_config(self):
        """Mock get_llm_config to return valid LLM config."""
        config = {
            "type": "openai",
            "apikey": "test-api-key",
            "apibase": "https://api.openai.com/v1",
            "model": "gpt-4o",
            "reasoning_effort": "none",
        }
        return config

    @pytest.fixture(autouse=True)
    def reset_session_cache(self):
        """Reset _cached_session before and after each test to prevent cross-test pollution."""
        import niu_api.internal.lightrag_manager as mgr
        mgr._cached_session = None
        mgr._cached_config_key = None
        yield
        mgr._cached_session = None
        mgr._cached_config_key = None

    async def test_basic_text_call_returns_string(self, mock_llm_config):
        """_llm_model_func with a simple prompt should return a string."""
        from unittest.mock import patch, MagicMock, AsyncMock

        # We need to create a _llm_model_func like LightRAG does in ensure_lightrag
        # but without actually creating a LightRAG instance.
        # Instead, we'll test by creating the function directly.

        from niu_api.internal.lightrag_manager import _get_lightrag_config

        # Build the _llm_model_func as it would be built in ensure_lightrag
        with patch("niu_api.internal.lightrag_manager._get_lightrag_config", return_value={}):
            with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
                # Mock LiteLLMSession.chat to return a simple response
                from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

                mock_response = MockResponse(thinking=None, content="Hello world", tool_calls=[], raw="Hello world")

                def mock_chat_generator(messages, tools=None, response_format=None):
                    """Simulate LiteLLMSession.chat() Generator behavior."""
                    yield "Hello world"
                    # Generator return value via StopIteration
                    return mock_response

                with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                    # Create the _llm_model_func
                    from niu_api.internal.lightrag_manager import _build_llm_model_func

                    func = _build_llm_model_func()

                    result = await func("What is Python?")
                    assert isinstance(result, str)
                    assert result == "Hello world"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestLlmModelFunc::test_basic_text_call_returns_string -v`
Expected: FAIL — `_build_llm_model_func` does not exist yet

- [ ] **Step 3: Write `_build_llm_model_func` and the new `_llm_model_func` implementation**

In `niu_api/internal/lightrag_manager.py`, make these changes:

**Delete these lines (old code):**

1. Lines 39-40: `PROXY_BASE_URL = "http://localhost:9876/llm/v1"` and `PROXY_API_KEY = "not-needed"`
2. Lines 43-48: `_shared_openai_client` and `_client_lock` global variables
3. Lines 51-70: `_get_shared_openai_client` function (entire function)
4. Lines 563-564: `from lightrag.llm.openai import openai_complete_if_cache` import
5. Lines 579-588: Old `_llm_model_func` async function (entire function body including `return await openai_complete_if_cache(...)`)

**Add these imports as lazy imports inside `_build_llm_model_func()` (NOT at module top-level):**

**WARNING: Do NOT add `from niu_api.internal.brain_region_prompt import ...` at the module top-level!** `brain_region_prompt.py` already imports `lightrag_manager.py` (`from niu_api.internal.lightrag_manager import get_brain_regions`), so a top-level import here would create a circular import cycle that crashes at startup. All imports must be lazy (inside function body).

**Add `_build_llm_model_func` function (after the imports section, before `_get_lightrag_config`):**

```python
# ============== LightRAG LLM Function Builder ==============

# Cache a shared LiteLLMSession instance keyed by config tuple.
# Avoids connection init overhead for high-frequency entity extraction calls.
_cached_session: Optional[Any] = None
_cached_config_key: Optional[tuple] = None
_session_lock = threading.Lock()


def _get_litellm_session(config: dict) -> Any:
    """Get or create a cached LiteLLMSession for LightRAG LLM calls.

    Config changes (model/api_base/api_key/api_type/reasoning_effort) trigger session rebuild.
    Thread-safe via double-check locking.
    """
    global _cached_session, _cached_config_key
    from agent.generic.litellm_adapter import LiteLLMSession

    config_key = (config.get("model"), config.get("apibase"), config.get("apikey"), config.get("type"), config.get("reasoning_effort"))

    if _cached_session is not None and _cached_config_key == config_key:
        return _cached_session

    with _session_lock:
        if _cached_session is not None and _cached_config_key == config_key:
            return _cached_session

        llm_config = {
            "api_type": config.get("type", "openai"),  # type -> api_type mapping
            "apikey": config["apikey"],
            "apibase": config["apibase"],
            "model": config["model"],
            "reasoning_effort": config.get("reasoning_effort"),
        }

        _cached_session = LiteLLMSession(cfg=llm_config)
        _cached_config_key = config_key
        logger.info("Created LiteLLMSession for LightRAG: model=%s, api_type=%s", config.get("model"), config.get("type"))
        return _cached_session


def _build_llm_model_func():
    """Build the async LLM function for LightRAG.

    Returns an async function that LightRAG calls for all LLM operations.
    Calls LiteLLMSession.chat() directly via asyncio.to_thread, avoiding
    OpenAI SDK compatibility issues and HTTP proxy overhead.

    Brain region injection is done here (not in proxy layer) for entity
    extraction requests.
    """
    from niu_api.llm_proxy import get_llm_config
    from agent.generic.litellm_adapter import LiteLLMSession, MockResponse
    from niu_api.internal.brain_region_prompt import (
        build_static_brain_region_prompt,
        build_dynamic_brain_region_prompt,
        BRAIN_REGION_MARKER,
    )

    async def _llm_model_func(
        prompt, system_prompt=None, history_messages=None,
        keyword_extraction=False, **kwargs,
    ) -> str:
        # 1. Pop LightRAG internal params (concurrency control, not for LLM)
        kwargs.pop("hashing_kv", None)
        kwargs.pop("_priority", None)
        kwargs.pop("_timeout", None)
        kwargs.pop("_queue_timeout", None)

        # 2. Brain region injection for entity extraction requests
        if system_prompt and BRAIN_REGION_MARKER in system_prompt:
            if "大脑区域架构" not in system_prompt:  # idempotent guard
                static_part = build_static_brain_region_prompt()
                dynamic_part = build_dynamic_brain_region_prompt()
                system_prompt = system_prompt + f"\n\n{static_part}\n\n{dynamic_part}"

        # 3. Build messages list
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        if history_messages:
            for msg in history_messages:
                content = msg.get("content") or ""  # litellm safety: None -> ""
                messages.append({"role": msg.get("role", "user"), "content": content})
        messages.append({"role": "user", "content": prompt})

        # 4. Get LLM config
        config = get_llm_config(use_lightrag_config=True)

        # 5. Handle keyword_extraction: build standard response_format dict
        response_format = None
        if keyword_extraction:
            from lightrag.types import GPTKeywordExtractionFormat
            schema = GPTKeywordExtractionFormat.model_json_schema()
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "keyword_extraction",
                    "strict": True,
                    "schema": schema,
                },
            }

        # 6. Handle enable_cot and stream from kwargs
        enable_cot = kwargs.pop("enable_cot", False)
        stream = kwargs.pop("stream", False)

        # 7. Call LiteLLMSession via asyncio.to_thread
        def sync_call():
            session = _get_litellm_session(config)
            gen = session.chat(messages=messages, response_format=response_format)

            # Consume generator
            chunks = []
            mock_response = None
            try:
                while True:
                    chunk = next(gen)
                    if isinstance(chunk, str):
                        chunks.append(chunk)
            except StopIteration as e:
                mock_response = e.value

            full_content = "".join(chunks)

            # Handle enable_cot (thinking chain)
            if enable_cot and mock_response and mock_response.thinking:
                if full_content:
                    # Content exists — ignore thinking, just return content
                    pass
                else:
                    # No content but thinking exists — wrap in think tags
                    full_content = f"<think>{mock_response.thinking}</think>\n"

            return full_content

        result = await asyncio.to_thread(sync_call)

        # 8. Stream handling
        if stream:
            # Pseudo-streaming: split complete result into chunks as AsyncIterator
            chunk_size = 20
            async def _async_gen():
                for i in range(0, max(len(result), 1), chunk_size):
                    yield result[i:i + chunk_size]
            return _async_gen()

        return result

    return _llm_model_func
```

**Update `ensure_lightrag` to use `_build_llm_model_func`:**

Replace the old `_llm_model_func` definition block (lines 575-590) with:

```python
    llm_model_func = _build_llm_model_func()
```

Also remove the `from lightrag.llm.openai import openai_complete_if_cache` import (line 563).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestLlmModelFunc::test_basic_text_call_returns_string -v`
Expected: PASS

- [ ] **Step 5: Write failing test for keyword_extraction response_format**

Add to `tests/test_lightrag_manager.py` in `TestLlmModelFunc` class:

```python
    async def test_keyword_extraction_builds_response_format(self, mock_llm_config):
        """keyword_extraction=True should build json_schema response_format for LiteLLMSession."""
        from unittest.mock import patch, MagicMock, call

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking=None, content='{"high_level_keywords": ["test"], "low_level_keywords": ["unit"]}', tool_calls=[], raw='{"high_level_keywords": ["test"], "low_level_keywords": ["unit"]}')

        captured_response_format = None

        original_chat = LiteLLMSession.chat

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_response_format
            captured_response_format = response_format
            yield mock_response.content
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func("test keywords", keyword_extraction=True)

        # Verify response_format was built correctly
        assert captured_response_format is not None
        assert captured_response_format["type"] == "json_schema"
        assert captured_response_format["json_schema"]["name"] == "keyword_extraction"
        assert captured_response_format["json_schema"]["strict"] is True
        assert "schema" in captured_response_format["json_schema"]
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestLlmModelFunc::test_keyword_extraction_builds_response_format -v`
Expected: PASS

- [ ] **Step 7: Write failing test for brain region injection**

Add to `tests/test_lightrag_manager.py` in `TestLlmModelFunc` class:

```python
    async def test_brain_region_injection_for_extraction_request(self, mock_llm_config):
        """Entity extraction requests should have brain region info injected into system_prompt."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking=None, content="Entity extraction result", tool_calls=[], raw="Entity extraction result")

        captured_messages = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_messages
            captured_messages = messages
            yield mock_response.content
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                with patch("niu_api.internal.brain_region_prompt.build_dynamic_brain_region_prompt",
                           return_value="当前图谱中的脑区：测试脑区、文档库脑区"):
                    from niu_api.internal.lightrag_manager import _build_llm_model_func

                    func = _build_llm_model_func()
                    result = await func(
                        "extract entities from this text",
                        system_prompt="---Role---\nYou are a Knowledge Graph Specialist...",
                    )

        # Verify brain region was injected
        system_msg = captured_messages[0]
        assert system_msg["role"] == "system"
        assert "大脑区域架构" in system_msg["content"]
        assert "测试脑区" in system_msg["content"]

    async def test_brain_region_not_injected_for_normal_request(self, mock_llm_config):
        """Normal LLM requests should NOT have brain region info injected."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking=None, content="Normal response", tool_calls=[], raw="Normal response")

        captured_messages = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_messages
            captured_messages = messages
            yield mock_response.content
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func(
                    "What is Python?",
                    system_prompt="You are a helpful assistant.",
                )

        system_msg = captured_messages[0]
        assert "大脑区域架构" not in system_msg["content"]

    async def test_brain_region_injection_idempotent(self, mock_llm_config):
        """If system_prompt already contains brain region info, skip injection (no double injection)."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking=None, content="Result", tool_calls=[], raw="Result")

        captured_messages = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_messages
            captured_messages = messages
            yield mock_response.content
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func(
                    "extract entities",
                    system_prompt="---Role---\nYou are a Knowledge Graph Specialist...\n\n大脑区域架构\nexisting content",
                )

        system_msg = captured_messages[0]
        # Count occurrences — should only be 1 (not 2)
        assert system_msg["content"].count("大脑区域架构") == 1
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestLlmModelFunc -v`
Expected: All tests PASS

- [ ] **Step 9: Write failing test for enable_cot handling**

Add to `tests/test_lightrag_manager.py` in `TestLlmModelFunc` class:

```python
    async def test_enable_cot_with_thinking_and_no_content(self, mock_llm_config):
        """When enable_cot=True and thinking exists but content is empty, wrap thinking in <think> tags."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking="Let me think about this...", content="", tool_calls=[], raw="")

        def mock_chat_generator(messages, tools=None, response_format=None):
            yield ""  # Empty content chunks
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func("test query", enable_cot=True)

        assert "<think>" in result
        assert "Let me think about this..." in result
        assert "</think>" in result

    async def test_enable_cot_with_thinking_and_content(self, mock_llm_config):
        """When enable_cot=True and both thinking and content exist, ignore thinking, return content only."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking="I need to calculate this...", content="The answer is 42", tool_calls=[], raw="The answer is 42")

        def mock_chat_generator(messages, tools=None, response_format=None):
            yield "The answer is 42"
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func("test query", enable_cot=True)

        assert result == "The answer is 42"
        assert "<think>" not in result

    async def test_stream_returns_async_iterator(self, mock_llm_config):
        """When stream=True, _llm_model_func should return an async generator (AsyncIterator)."""
        from unittest.mock import patch
        from collections.abc import AsyncIterator

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking=None, content="Streamed content here", tool_calls=[], raw="Streamed content here")

        def mock_chat_generator(messages, tools=None, response_format=None):
            yield "Streamed content here"
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func("test query", stream=True)

        assert isinstance(result, AsyncIterator)

        # Consume the async iterator and verify content
        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        full = "".join(chunks)
        assert full == "Streamed content here"
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestLlmModelFunc -v`
Expected: All tests PASS

- [ ] **Step 11: Write test for LightRAG internal params being popped**

Add to `tests/test_lightrag_manager.py` in `TestLlmModelFunc` class:

```python
    async def test_lightrag_internal_params_popped(self, mock_llm_config):
        """LightRAG concurrency control params should be silently removed."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking=None, content="OK", tool_calls=[], raw="OK")

        captured_messages = None
        captured_response_format = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_messages, captured_response_format
            captured_messages = messages
            captured_response_format = response_format
            yield "OK"
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                # Pass LightRAG internal params — they should be silently discarded
                result = await func(
                    "test",
                    hashing_kv="some_cache",
                    _priority=1,
                    _timeout=30,
                    _queue_timeout=10,
                )

        # These params should NOT appear in messages or response_format
        for msg in captured_messages:
            assert "hashing_kv" not in str(msg)
            assert "_priority" not in str(msg)
```

- [ ] **Step 12: Run tests to verify they pass**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestLlmModelFunc -v`
Expected: PASS

- [ ] **Step 13: Write test for history_messages with content=None**

Add to `tests/test_lightrag_manager.py` in `TestLlmModelFunc` class:

```python
    async def test_history_messages_content_none_replaced_with_empty(self, mock_llm_config):
        """history_messages with content=None should be converted to empty string."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking=None, content="OK", tool_calls=[], raw="OK")

        captured_messages = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_messages
            captured_messages = messages
            yield "OK"
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func(
                    "test",
                    history_messages=[
                        {"role": "user", "content": None},  # Should become ""
                    ],
                )

        # Find the history message — it comes before the prompt message
        # Messages order: [history_msg, prompt_msg]
        history_msg = [m for m in captured_messages if m["content"] == ""]
        assert len(history_msg) >= 1, f"Expected a message with empty content, got: {captured_messages}"
```

- [ ] **Step 14: Run tests to verify they pass**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestLlmModelFunc -v`
Expected: PASS

- [ ] **Step 15: Run all lightrag_manager tests together**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py -v`
Expected: All tests PASS (old + new)

- [ ] **Step 16: Commit**

```bash
git add niu_api/internal/lightrag_manager.py tests/test_lightrag_manager.py
git commit -m "feat: rewrite _llm_model_func to call LiteLLMSession directly via asyncio.to_thread"
```

---

### Task 3: lightrag_manager.py — delete old code and update docstrings/status

**Files:**
- Modify: `niu_api/internal/lightrag_manager.py`

**Why third:** Task 2 already added the new `_llm_model_func`. This task cleans up the old code that's no longer needed (PROXY_BASE_URL, PROXY_API_KEY, _get_shared_openai_client, proxy_base_url in status).

**Pre-requisite:** Task 2 must be completed.

- [ ] **Step 1: Write failing test — verify PROXY_BASE_URL is removed**

Add to `tests/test_lightrag_manager.py`:

```python
    def test_no_proxy_base_url_constant(self):
        """PROXY_BASE_URL should not exist — LightRAG calls LiteLLMSession directly."""
        import niu_api.internal.lightrag_manager as lm
        assert not hasattr(lm, "PROXY_BASE_URL"), "PROXY_BASE_URL should be deleted"

    def test_no_proxy_api_key_constant(self):
        """PROXY_API_KEY should not exist."""
        import niu_api.internal.lightrag_manager as lm
        assert not hasattr(lm, "PROXY_API_KEY"), "PROXY_API_KEY should be deleted"

    def test_no_shared_openai_client(self):
        """_get_shared_openai_client should not exist."""
        import niu_api.internal.lightrag_manager as lm
        assert not hasattr(lm, "_get_shared_openai_client"), "_get_shared_openai_client should be deleted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestConfig::test_no_proxy_base_url_constant -v`
Expected: FAIL — `PROXY_BASE_URL` still exists

- [ ] **Step 3: Delete the old code**

In `niu_api/internal/lightrag_manager.py`:

1. Delete lines 39-40: `PROXY_BASE_URL` and `PROXY_API_KEY` constants
2. Delete lines 43-48: `_shared_openai_client` and `_client_lock` globals
3. Delete lines 51-70: `_get_shared_openai_client` function
4. Delete `from lightrag.llm.openai import openai_complete_if_cache` import (in the try block around line 563-564)
5. Update module docstring (lines 8-9): Change `routed through /llm/v1/ proxy (→ LiteLLM → user-config.json)` to `direct LiteLLMSession.chat() (→ LiteLLM → user-config.json)`

In `get_lightrag_status()` function (line 775):
6. Delete `"proxy_base_url": PROXY_BASE_URL,` line

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestConfig::test_no_proxy_base_url_constant tests/test_lightrag_manager.py::TestConfig::test_no_proxy_api_key_constant tests/test_lightrag_manager.py::TestConfig::test_no_shared_openai_client -v`
Expected: All PASS

- [ ] **Step 5: Write failing test — verify proxy_base_url removed from status**

Add to `tests/test_lightrag_manager.py`:

```python
    def test_status_dict_no_proxy_base_url_key(self):
        """get_lightrag_status() should NOT contain proxy_base_url key."""
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert "proxy_base_url" not in status, "proxy_base_url should be removed from status dict"
```

- [ ] **Step 6: Run test to verify it passes (already fixed in Step 3)**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py::TestStatus::test_status_dict_no_proxy_base_url_key -v`
Expected: PASS

- [ ] **Step 7: Delete old `test_proxy_base_url` test**

In `tests/test_lightrag_manager.py`, delete:
- Line 18-21: `test_proxy_base_url` method (tests `PROXY_BASE_URL` which no longer exists)
- Line 76: `assert "proxy_base_url" in status` assertion in `test_returns_dict_with_required_keys`

- [ ] **Step 8: Run all lightrag_manager tests**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add niu_api/internal/lightrag_manager.py tests/test_lightrag_manager.py
git commit -m "refactor: delete PROXY_BASE_URL, _get_shared_openai_client, and proxy_base_url from status"
```

---

### Task 4: llm_proxy.py — delete chat_completions and embeddings endpoints, delete brain region interception

**Files:**
- Modify: `niu_api/llm_proxy.py`

**Why fourth:** The proxy endpoints are no longer called by anyone (LightRAG now calls LiteLLMSession directly). Delete the unused routes and the brain region interception that has moved to `_llm_model_func`.

**Pre-requisite:** Task 2 must be completed (LightRAG no longer uses the proxy).

- [ ] **Step 1: Write failing test — verify endpoints are gone**

Add to `tests/test_llm_proxy.py`:

```python
class TestEndpointRemoval:
    """Verify that removed endpoints no longer exist."""

    def test_chat_completions_endpoint_removed(self):
        """The /llm/v1/chat/completions POST endpoint should no longer exist."""
        from niu_api.llm_proxy import router
        routes = [r.path for r in router.routes]
        # No route should match "/chat/completions"
        assert "/chat/completions" not in routes

    def test_embeddings_endpoint_removed(self):
        """The /llm/v1/embeddings POST endpoint should no longer exist."""
        from niu_api.llm_proxy import router
        routes = [r.path for r in router.routes]
        assert "/embeddings" not in routes

    def test_remaining_endpoints_still_exist(self):
        """Health, models, and status endpoints should still exist."""
        from niu_api.llm_proxy import router
        routes = [r.path for r in router.routes]
        assert "/health" in routes
        assert "/models" in routes
        assert "/status" in routes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python -m pytest tests/test_llm_proxy.py::TestEndpointRemoval -v`
Expected: FAIL — `/chat/completions` and `/embeddings` still exist in routes

- [ ] **Step 3: Delete the unused code in llm_proxy.py**

In `niu_api/llm_proxy.py`:

1. Delete line 27: `from niu_api.internal.brain_region_prompt import inject_brain_region_context, is_lightrag_extraction_request`
2. Delete the entire `chat_completions` endpoint function (lines 378-447) — from `@router.post("/chat/completions")` through the end of the function
3. Delete the entire embeddings endpoint section (lines 504-528+) — `class OpenAIEmbeddingRequest`, `@router.post("/embeddings")`, and `create_embeddings` function
4. Delete brain region interception code inside `chat_completions` (already deleted with the function in step 2, but verify it's gone)
5. Delete `is_lightrag` detection and routing logic (already deleted with the function)
6. Update module docstring (lines 1-17): Replace with:

```python
"""
LLM Proxy Utilities

Provides helper functions for LLM configuration and direct LLM calls
through LiteLLMSession. Used by MCP client sampling callbacks and
LightRAG's _llm_model_func.

Remaining HTTP endpoints:
- GET /llm/v1/models — list configured model
- GET /llm/v1/health — check if LLM is configured
- GET /llm/v1/status — LightRAG and model status
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python -m pytest tests/test_llm_proxy.py::TestEndpointRemoval -v`
Expected: All 3 tests PASS

- [ ] **Step 5: Delete old test classes that test removed endpoints**

In `tests/test_llm_proxy.py`:
1. Delete `class TestChatCompletions` (lines 114-205) — all 4 test methods
2. Delete `class TestEmbeddingsEndpoint` (lines 207-267) — all 4 test methods

Keep everything else: `TestHealthEndpoint`, `TestModelsEndpoint`, `TestStatusEndpoint`, `TestFormatConversion`.

- [ ] **Step 6: Delete entire `test_llm_proxy_injection.py` file**

This file tests brain region injection at the proxy layer, which has been moved to `_llm_model_func`. All 3 test functions are now invalid.

```bash
rm tests/test_llm_proxy_injection.py
```

- [ ] **Step 7: Run remaining proxy tests**

Run: `cd <repo_root> && python -m pytest tests/test_llm_proxy.py -v`
Expected: Only `TestHealthEndpoint`, `TestModelsEndpoint`, `TestStatusEndpoint`, `TestFormatConversion`, and `TestEndpointRemoval` tests remain — all PASS

- [ ] **Step 8: Verify MCP client still works**

Run: `cd <repo_root> && python -c "from niu_api.llm_proxy import get_llm_config, call_llm_via_litellm; print('MCP imports OK')"`
Expected: `MCP imports OK` — no import errors

- [ ] **Step 9: Commit**

```bash
git add niu_api/llm_proxy.py tests/test_llm_proxy.py
git rm tests/test_llm_proxy_injection.py
git commit -m "refactor: delete unused proxy endpoints and brain region interception from llm_proxy.py"
```

---

### Task 5: Integration verification — syntax check and import validation

**Files:**
- All modified files

**Why last:** This is the final validation step to ensure no import errors, no syntax issues, and the program can start up.

**Pre-requisite:** Tasks 1-4 must be completed.

- [ ] **Step 1: Syntax check all modified files**

Run: `cd <repo_root> && python -m py_compile niu_api/internal/lightrag_manager.py && python -m py_compile niu_api/llm_proxy.py && python -m py_compile agent/generic/litellm_adapter.py && echo "All files compile OK"`
Expected: All files compile without errors

- [ ] **Step 2: Import chain validation**

Run: `cd <repo_root> && python -c "
from niu_api.internal.lightrag_manager import _build_llm_model_func, get_lightrag_status, get_brain_regions
from niu_api.llm_proxy import get_llm_config, call_llm_via_litellm, router
from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt, build_dynamic_brain_region_prompt
from agent.generic.litellm_adapter import LiteLLMSession
print('All imports OK')
status = get_lightrag_status()
assert 'proxy_base_url' not in status
print('Status dict has no proxy_base_url: OK')
"`
Expected: `All imports OK` and `Status dict has no proxy_base_url: OK`

- [ ] **Step 3: Verify __main__.py router registration is intact**

Run: `cd <repo_root> && python -c "from niu_api.__main__ import app; routes = [r.path for r in app.routes]; llm_routes = [r for r in routes if '/llm/v1' in r]; print(f'LLM routes: {llm_routes}')"`
Expected: LLM routes still include `/llm/v1/models`, `/llm/v1/health`, `/llm/v1/status` but NOT `/llm/v1/chat/completions` or `/llm/v1/embeddings`

- [ ] **Step 4: Run full test suite**

Run: `cd <repo_root> && python -m pytest tests/test_lightrag_manager.py tests/test_llm_proxy.py tests/test_litellm_adapter_drop_params.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit (final verification commit)**

```bash
git add -A
git commit -m "verify: integration check — all imports, routes, and tests pass after LightRAG SDK integration"
```

---

## Self-Review Checklist

**1. Spec coverage:**

| Spec requirement | Task |
|-----------------|------|
| `_llm_model_func` calls LiteLLMSession directly | Task 2 |
| `asyncio.to_thread` bridge | Task 2 (sync_call inside `_llm_model_func`) |
| Brain region injection in `_llm_model_func` | Task 2 |
| Idempotent brain region guard | Task 2 |
| keyword_extraction response_format | Task 2 |
| drop_params=True for response_format | Task 1 |
| LiteLLMSession instance caching | Task 2 (_get_litellm_session) |
| type → api_type mapping | Task 2 (config mapping in _get_litellm_session) |
| Pop LightRAG internal params | Task 2 |
| content=None → "" conversion | Task 2 |
| enable_cot handling | Task 2 |
| Stream pseudo-streaming | Task 2 |
| Delete PROXY_BASE_URL/PROXY_API_KEY | Task 3 |
| Delete _get_shared_openai_client | Task 3 |
| Delete proxy_base_url from status | Task 3 |
| Delete chat_completions endpoint | Task 4 |
| Delete embeddings endpoint | Task 4 |
| Delete brain region import from llm_proxy | Task 4 |
| Delete brain region interception code | Task 4 |
| Update llm_proxy docstring | Task 4 |
| Delete test_llm_proxy_injection.py | Task 4 |
| Update test_lightrag_manager.py | Task 3 |
| Update test_llm_proxy.py | Task 4 |

**2. Placeholder scan:** No TBD, TODO, or "implement later" found. All steps contain complete code.

**3. Type consistency:** `_build_llm_model_func()` returns `async def _llm_model_func(...)` — matches LightRAG's expected signature. `GPTKeywordExtractionFormat.model_json_schema()` returns dict — used as `schema` value in `response_format` dict. `LiteLLMSession` constructed with `cfg` dict containing `api_type` key — matches `LiteLLMSession.__init__` which reads `cfg.get("api_type", "openai")`.

All spec requirements covered, no placeholders, types consistent.
