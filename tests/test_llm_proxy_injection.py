"""Integration tests for brain region injection in LLM proxy."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_inject_function_called_in_proxy():
    """Verify inject_brain_region_context is called in the proxy pipeline."""
    # Import the proxy module
    from niu_api.llm_proxy import chat_completions, OpenAIChatRequest, OpenAIMessage

    request = OpenAIChatRequest(
        model="test-model",
        messages=[
            OpenAIMessage(
                role="system",
                content="---Role---\nYou are a Knowledge Graph Specialist...",
            ),
            OpenAIMessage(role="user", content="Extract entities from: test text"),
        ],
    )

    # Mock everything needed for the proxy to work
    with patch("niu_api.llm_proxy.call_llm_via_litellm", new=AsyncMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "test-key", "apibase": "http://test", "model": "test-model"
        }):
            with patch("niu_api.llm_proxy.inject_brain_region_context") as mock_inject:
                mock_inject.return_value = [
                    {"role": "system", "content": "Knowledge Graph Specialist... + brain region info"},
                    {"role": "user", "content": "Extract entities from: test text"},
                ]
                with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls:
                    mock_adapter_cls.return_value = MagicMock()
                    response = await chat_completions(request)

    # Verify inject_brain_region_context was called
    mock_inject.assert_called_once()


@pytest.mark.asyncio
async def test_injection_not_called_for_normal_chat():
    """Normal chat requests still go through inject_brain_region_context (it checks internally)."""
    from niu_api.llm_proxy import chat_completions, OpenAIChatRequest, OpenAIMessage

    request = OpenAIChatRequest(
        model="test-model",
        messages=[
            OpenAIMessage(role="system", content="You are a helpful assistant."),
            OpenAIMessage(role="user", content="Hello"),
        ],
    )

    with patch("niu_api.llm_proxy.call_llm_via_litellm", new=AsyncMock(return_value={
        "choices": [{"message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "test-key", "apibase": "http://test", "model": "test-model"
        }):
            with patch("niu_api.llm_proxy.inject_brain_region_context") as mock_inject:
                # For non-extraction requests, inject returns the same messages unchanged
                mock_inject.return_value = [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ]
                with patch("niu_api.internal.lightrag_adapter.LightRAGAdapter") as mock_adapter_cls:
                    mock_adapter_cls.return_value = MagicMock()
                    response = await chat_completions(request)

    # inject is still called (it checks internally whether to inject or not)
    mock_inject.assert_called_once()
