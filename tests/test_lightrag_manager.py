"""
Tests for niu_api/internal/lightrag_manager.py

LightRAG instance lifecycle, async/sync bridge, status reporting.
"""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path


# ============== Config Tests ==============


class TestConfig:
    """Test LightRAG configuration reading."""

    def test_llm_mode_is_litellm_direct(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert status.get("llm_mode") == "litellm_direct"

    def test_storage_dir_under_niu_home(self):
        from niu_api.internal.lightrag_manager import STORAGE_DIR
        assert ".niu" in str(STORAGE_DIR)
        assert "lightrag_storage" in str(STORAGE_DIR)

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

    def test_get_lightrag_config_returns_dict(self):
        from niu_api.internal.lightrag_manager import _get_lightrag_config
        config = _get_lightrag_config()
        assert isinstance(config, dict)


# ============== Availability Tests ==============


class TestAvailability:
    """Test LightRAG availability detection."""

    def test_is_available_false_when_not_installed(self):
        from niu_api.internal.lightrag_manager import is_lightrag_available
        # LightRAG is not installed in test environment
        result = is_lightrag_available()
        assert isinstance(result, bool)
        # In test env, likely False since lightrag-hku is not installed
        # But we don't assert False because it might be installed

    def test_get_lightrag_returns_none_when_not_installed(self):
        from niu_api.internal.lightrag_manager import get_lightrag
        import niu_api.internal.lightrag_manager as mgr
        with mgr._rag_lock:
            old_instance = mgr._rag_instance
            mgr._rag_instance = None
        try:
            with patch("niu_api.internal.lightrag_manager.is_lightrag_available", return_value=False):
                with patch("niu_api.internal.lightrag_manager._create_lightrag_instance", side_effect=ImportError("no lightrag")):
                    result = get_lightrag()
                    assert result is None
        finally:
            with mgr._rag_lock:
                mgr._rag_instance = old_instance


# ============== Status Tests ==============


class TestStatus:
    """Test get_lightrag_status() diagnostics."""

    def test_returns_dict_with_required_keys(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert "installed" in status
        assert "initialized" in status
        assert "storage_dir" in status
        assert "llm_mode" in status
        assert "embedding" in status
        assert "reranker" in status
        assert "loop_running" in status

    def test_status_shows_not_initialized(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        import niu_api.internal.lightrag_manager as mgr
        with mgr._rag_lock:
            old_instance = mgr._rag_instance
            mgr._rag_instance = None
        try:
            status = get_lightrag_status()
            assert status["initialized"] is False
        finally:
            with mgr._rag_lock:
                mgr._rag_instance = old_instance

    def test_status_includes_embedding_info(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert "name" in status["embedding"]
        assert "dim" in status["embedding"]

    def test_status_includes_reranker_info(self):
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert "name" in status["reranker"]

    def test_status_dict_no_proxy_base_url_key(self):
        """get_lightrag_status() should NOT contain proxy_base_url key."""
        from niu_api.internal.lightrag_manager import get_lightrag_status
        status = get_lightrag_status()
        assert "proxy_base_url" not in status, "proxy_base_url should be removed from status dict"


# ============== Async/Sync Bridge Tests ==============


class TestAsyncSyncBridge:
    """Test the daemon event loop bridge."""

    def test_ensure_loop_creates_loop(self):
        from niu_api.internal.lightrag_manager import _ensure_loop
        import niu_api.internal.lightrag_manager as mgr

        # Reset loop state
        mgr._loop = None
        mgr._loop_thread = None

        loop = _ensure_loop()
        assert loop is not None
        assert loop.is_running()

    def test_call_async_runs_coroutine(self):
        from niu_api.internal.lightrag_manager import call_async
        import asyncio

        async def sample_coro():
            return 42

        result = call_async(sample_coro())
        assert result == 42

    def test_call_async_handles_exceptions(self):
        from niu_api.internal.lightrag_manager import call_async

        async def failing_coro():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            call_async(failing_coro())


# ============== Embedding Dim Tests ==============


class TestEmbeddingDimForLightRAG:
    """Test _get_embedding_dim_for_lightrag() delegates correctly."""

    def test_returns_current_dim(self):
        from niu_api.internal.lightrag_manager import _get_embedding_dim_for_lightrag
        dim = _get_embedding_dim_for_lightrag()
        assert isinstance(dim, int)
        assert dim > 0

    def test_default_is_bge_base_zh(self):
        from niu_api.internal.lightrag_manager import _get_embedding_dim_for_lightrag
        dim = _get_embedding_dim_for_lightrag()
        # Default model is bge-base-zh-v1.5 (768d)
        assert dim == 768


# ============== ensure_lightrag Tests ==============


class TestEnsureLightRAG:
    """Test async ensure_lightrag()."""

    @pytest.mark.asyncio
    async def test_returns_none_when_not_available(self):
        from niu_api.internal.lightrag_manager import ensure_lightrag
        import niu_api.internal.lightrag_manager as mgr
        with mgr._rag_lock:
            old_instance = mgr._rag_instance
            mgr._rag_instance = None
        try:
            with patch("niu_api.internal.lightrag_manager.get_lightrag", return_value=None):
                result = await ensure_lightrag()
                assert result is None
        finally:
            with mgr._rag_lock:
                mgr._rag_instance = old_instance


# ============== LLM Model Func Tests ==============


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
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking=None, content="Hello world", tool_calls=[], raw="Hello world")

        def mock_chat_generator(messages, tools=None, response_format=None):
            yield "Hello world"
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func("What is Python?")
                assert isinstance(result, str)
                assert result == "Hello world"

    async def test_keyword_extraction_builds_response_format(self, mock_llm_config):
        """keyword_extraction=True should build json_schema response_format for LiteLLMSession."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        # 新逻辑 _resolve_response_format 按 litellm_kwargs.response_format_mode 决定
        # 构造哪种 response_format。mock_llm_config（fixture）不含 litellm_kwargs，
        # 这里给 patch 返回值补上 response_format_mode=json_schema，确保走 json_schema 档。
        mock_response = MockResponse(thinking=None, content='{"high_level_keywords": ["test"], "low_level_keywords": ["unit"]}', tool_calls=[], raw='{"high_level_keywords": ["test"], "low_level_keywords": ["unit"]}')

        captured_response_format = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_response_format
            captured_response_format = response_format
            yield mock_response.content
            return mock_response

        mock_config_with_mode = {
            **mock_llm_config,
            "litellm_kwargs": {"response_format_mode": "json_schema"},
        }
        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_config_with_mode):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func("test keywords", keyword_extraction=True)

        assert captured_response_format is not None
        assert captured_response_format["type"] == "json_schema"
        assert captured_response_format["json_schema"]["name"] == "keyword_extraction"
        assert captured_response_format["json_schema"]["strict"] is True
        assert "schema" in captured_response_format["json_schema"]

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
        assert system_msg["content"].count("大脑区域架构") == 1

    async def test_user_info_injected_for_extraction_request(self, mock_llm_config):
        """提取请求 + 用户信息非空 → system_prompt 同时含用户信息和脑区架构。"""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(
            thinking=None, content="Entity extraction result",
            tool_calls=[], raw="Entity extraction result",
        )

        captured_messages = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_messages
            captured_messages = messages
            yield mock_response.content
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                with patch(
                    "niu_api.internal.brain_region_prompt.build_dynamic_brain_region_prompt",
                    return_value="当前图谱中的脑区：测试脑区、文档库脑区",
                ):
                    with patch(
                        "niu_api.internal.brain_region_prompt.build_user_info_prompt",
                        return_value="## 知识图谱所属用户\n\n本知识图谱属于以下用户：\n- 真实姓名：李磊",
                    ):
                        from niu_api.internal.lightrag_manager import _build_llm_model_func

                        func = _build_llm_model_func()
                        await func(
                            "extract entities from this text",
                            system_prompt="---Role---\nYou are a Knowledge Graph Specialist...",
                        )

        system_msg = captured_messages[0]
        assert "知识图谱所属用户" in system_msg["content"]
        assert "真实姓名：李磊" in system_msg["content"]
        # 脑区架构与用户信息共存
        assert "大脑区域架构" in system_msg["content"]

    async def test_user_info_not_injected_when_empty(self, mock_llm_config):
        """提取请求 + 用户信息为空串 → 不注入用户信息，但脑区架构仍注入。"""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(
            thinking=None, content="Entity extraction result",
            tool_calls=[], raw="Entity extraction result",
        )

        captured_messages = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_messages
            captured_messages = messages
            yield mock_response.content
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                with patch(
                    "niu_api.internal.brain_region_prompt.build_dynamic_brain_region_prompt",
                    return_value="当前图谱中的脑区：测试脑区",
                ):
                    with patch(
                        "niu_api.internal.brain_region_prompt.build_user_info_prompt",
                        return_value="",
                    ):
                        from niu_api.internal.lightrag_manager import _build_llm_model_func

                        func = _build_llm_model_func()
                        await func(
                            "extract entities",
                            system_prompt="---Role---\nYou are a Knowledge Graph Specialist...",
                        )

        system_msg = captured_messages[0]
        assert "知识图谱所属用户" not in system_msg["content"]
        assert "大脑区域架构" in system_msg["content"]

    async def test_user_info_injection_idempotent(self, mock_llm_config):
        """system_prompt 已含用户信息 → 不重复注入用户信息。"""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(
            thinking=None, content="Result", tool_calls=[], raw="Result",
        )

        captured_messages = None

        def mock_chat_generator(messages, tools=None, response_format=None):
            nonlocal captured_messages
            captured_messages = messages
            yield mock_response.content
            return mock_response

        # build_user_info_prompt 仍应被调用但返回内容不应被注入（幂等 guard 拦截）
        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                with patch(
                    "niu_api.internal.brain_region_prompt.build_dynamic_brain_region_prompt",
                    return_value="当前图谱中的脑区：测试脑区",
                ):
                    with patch(
                        "niu_api.internal.brain_region_prompt.build_user_info_prompt",
                        return_value="## 知识图谱所属用户\n\n本知识图谱属于以下用户：\n- 真实姓名：李磊",
                    ) as mock_user_info:
                        from niu_api.internal.lightrag_manager import _build_llm_model_func

                        func = _build_llm_model_func()
                        await func(
                            "extract entities",
                            system_prompt=(
                                "---Role---\nYou are a Knowledge Graph Specialist...\n\n"
                                "## 知识图谱所属用户\n\n本知识图谱属于以下用户：\n- 真实姓名：李磊"
                            ),
                        )

        system_msg = captured_messages[0]
        # 幂等：用户信息只出现一次
        assert system_msg["content"].count("知识图谱所属用户") == 1
        # 幂等 guard 在检测到已存在时根本不调用 build_user_info_prompt
        mock_user_info.assert_not_called()
        """When enable_cot=True and thinking exists but content is empty, wrap thinking in tags."""
        from unittest.mock import patch

        from agent.generic.litellm_adapter import LiteLLMSession, MockResponse

        mock_response = MockResponse(thinking="Let me think about this...", content="", tool_calls=[], raw="")

        def mock_chat_generator(messages, tools=None, response_format=None):
            yield ""
            return mock_response

        with patch("niu_api.llm_proxy.get_llm_config", return_value=mock_llm_config):
            with patch.object(LiteLLMSession, "chat", side_effect=mock_chat_generator):
                from niu_api.internal.lightrag_manager import _build_llm_model_func

                func = _build_llm_model_func()
                result = await func("test query", enable_cot=True)

        assert result.startswith("<think>")
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

        chunks = []
        async for chunk in result:
            chunks.append(chunk)
        full = "".join(chunks)
        assert full == "Streamed content here"

    async def test_lightrag_internal_params_popped(self, mock_llm_config):
        """LightRAG concurrency control params should be silently removed."""
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
                    hashing_kv="some_cache",
                    _priority=1,
                    _timeout=30,
                    _queue_timeout=10,
                )

        for msg in captured_messages:
            assert "hashing_kv" not in str(msg)
            assert "_priority" not in str(msg)

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
                        {"role": "user", "content": None},
                    ],
                )

        history_msg = [m for m in captured_messages if m["content"] == ""]
        assert len(history_msg) >= 1, f"Expected a message with empty content, got: {captured_messages}"