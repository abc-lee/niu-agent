"""Integration tests for brain region injection in LLM proxy."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_proxy_injects_brain_region_for_extraction_request():
    """LightRAG extraction requests get brain region context injected into messages."""
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

    # Mock the LLM call to capture what messages are actually sent
    captured_messages = {}

    async def mock_call_llm(messages, **kwargs):
        captured_messages["messages"] = messages
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "done"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # Mock get_brain_regions so real inject_brain_region_context runs with regions
    with patch("niu_api.llm_proxy.call_llm_via_litellm", new=AsyncMock(side_effect=mock_call_llm)):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "test-key", "apibase": "http://test", "model": "test-model"
        }):
            with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=["聊天历史脑区", "文档库脑区"]):
                response = await chat_completions(request)

    # Verify the LLM received messages with brain region context
    sent_messages = captured_messages["messages"]
    system_msg = next(m for m in sent_messages if m["role"] == "system")
    assert "niu" in system_msg["content"], "Brain region architecture should be in system message"
    assert "聊天历史" in system_msg["content"], "Dynamic brain regions should be in system message"
    assert "Knowledge Graph Specialist" in system_msg["content"], "Original content preserved"


@pytest.mark.asyncio
async def test_proxy_skips_injection_for_normal_chat():
    """Normal chat requests are NOT modified -- inject returns same messages."""
    from niu_api.llm_proxy import chat_completions, OpenAIChatRequest, OpenAIMessage

    request = OpenAIChatRequest(
        model="test-model",
        messages=[
            OpenAIMessage(role="system", content="You are a helpful assistant."),
            OpenAIMessage(role="user", content="Hello"),
        ],
    )

    captured_messages = {}

    async def mock_call_llm(messages, **kwargs):
        captured_messages["messages"] = messages
        return {
            "choices": [{"message": {"role": "assistant", "content": "Hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    with patch("niu_api.llm_proxy.call_llm_via_litellm", new=AsyncMock(side_effect=mock_call_llm)):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "test-key", "apibase": "http://test", "model": "test-model"
        }):
            with patch("niu_api.internal.brain_region_prompt.get_brain_regions", return_value=["聊天历史脑区", "文档库脑区"]):
                response = await chat_completions(request)

    # Verify no brain region content in normal chat
    sent_messages = captured_messages["messages"]
    system_msg = next(m for m in sent_messages if m["role"] == "system")
    assert "大脑区域架构" not in system_msg["content"], "Brain region should NOT be injected for normal chat"
    assert system_msg["content"] == "You are a helpful assistant.", "Original content unchanged"


@pytest.mark.asyncio
async def test_proxy_gracefully_handles_injection_failure():
    """If injection fails, proxy continues without it -- no crash."""
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

    captured_messages = {}

    async def mock_call_llm(messages, **kwargs):
        captured_messages["messages"] = messages
        return {
            "choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # Make get_brain_regions throw an exception so injection fails
    with patch("niu_api.llm_proxy.call_llm_via_litellm", new=AsyncMock(side_effect=mock_call_llm)):
        with patch("niu_api.llm_proxy.get_llm_config", return_value={
            "type": "openai", "apikey": "test-key", "apibase": "http://test", "model": "test-model"
        }):
            with patch("niu_api.internal.brain_region_prompt.get_brain_regions", side_effect=Exception("LightRAG not initialized")):
                response = await chat_completions(request)

    # Verify request still succeeded (no crash)
    assert response is not None
    # When get_brain_regions fails, inject_brain_region_context still injects
    # the static prompt + fallback regions (graceful degradation, not skip).
    sent_messages = captured_messages["messages"]
    system_msg = next(m for m in sent_messages if m["role"] == "system")
    # Static brain region prompt is still injected (with fallback regions)
    assert "大脑区域架构" in system_msg["content"], "Static brain region prompt should still be injected with fallback"
    assert "Knowledge Graph Specialist" in system_msg["content"], "Original content preserved"
