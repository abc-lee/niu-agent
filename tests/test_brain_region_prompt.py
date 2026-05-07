"""Tests for brain region prompt injection into LightRAG LLM requests."""
import pytest
from niu_api.internal.brain_region_prompt import is_lightrag_extraction_request


def test_is_lightrag_extraction_request_with_specialist():
    """System prompt containing 'Knowledge Graph Specialist' is detected."""
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities from this text..."},
    ]
    assert is_lightrag_extraction_request(messages) is True


def test_is_lightrag_extraction_request_without_specialist():
    """Normal chat messages are NOT detected as extraction requests."""
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello, how are you?"},
    ]
    assert is_lightrag_extraction_request(messages) is False


def test_is_lightrag_extraction_request_empty_messages():
    """Empty message list is not an extraction request."""
    assert is_lightrag_extraction_request([]) is False


def test_is_lightrag_extraction_request_no_system():
    """Messages without system prompt are not extraction requests."""
    messages = [
        {"role": "user", "content": "Hello"},
    ]
    assert is_lightrag_extraction_request(messages) is False


def test_is_lightrag_extraction_request_system_no_content():
    """System message without 'content' key is not an extraction request."""
    messages = [
        {"role": "system"},
        {"role": "user", "content": "Hello"},
    ]
    assert is_lightrag_extraction_request(messages) is False


def test_is_lightrag_extraction_request_system_empty_content():
    """System message with empty content is not an extraction request."""
    messages = [
        {"role": "system", "content": ""},
        {"role": "user", "content": "Hello"},
    ]
    assert is_lightrag_extraction_request(messages) is False


def test_build_static_brain_region_prompt_returns_string():
    """Static prompt is a non-empty string."""
    from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt
    result = build_static_brain_region_prompt()
    assert isinstance(result, str)
    assert len(result) > 0


def test_build_static_brain_region_prompt_contains_key_concepts():
    """Static prompt contains all key brain region concepts."""
    from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt
    result = build_static_brain_region_prompt()
    # Must contain these key terms
    assert "brain:Niu" in result
    assert "brain_region_anchor" in result
    assert "belongs_to_region" in result
    assert "聊天历史" in result
    assert "文档库" in result
    assert "知识体系" in result
    assert "brain:region:" in result


def test_build_static_brain_region_prompt_consistent():
    """Calling twice returns the same content (pure function, no side effects)."""
    from niu_api.internal.brain_region_prompt import build_static_brain_region_prompt
    result1 = build_static_brain_region_prompt()
    result2 = build_static_brain_region_prompt()
    assert result1 == result2


from unittest.mock import MagicMock


def test_build_dynamic_brain_region_prompt_with_regions():
    """Dynamic prompt includes current brain regions from graph."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    mock_adapter = MagicMock()
    mock_adapter.query.return_value = (
        "brain:region:聊天历史 - 聊天记录和对话历史\n"
        "brain:region:文档库 - 文档和文件存储\n"
        "brain:region:知识体系 - 系统化知识\n"
    )

    prompt = build_dynamic_brain_region_prompt(mock_adapter)
    assert "聊天历史" in prompt
    assert "文档库" in prompt
    assert "知识体系" in prompt
    assert "当前图谱中的脑区" in prompt


def test_build_dynamic_brain_region_prompt_empty():
    """When no regions found, dynamic prompt returns fallback."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    mock_adapter = MagicMock()
    mock_adapter.query.return_value = ""

    prompt = build_dynamic_brain_region_prompt(mock_adapter)
    assert "默认" in prompt, f"Expected fallback marker '默认' in prompt, got: {prompt!r}"


def test_build_dynamic_brain_region_prompt_adapter_failure():
    """When adapter raises exception, falls back to defaults."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    mock_adapter = MagicMock()
    mock_adapter.query.side_effect = Exception("LightRAG not initialized")

    prompt = build_dynamic_brain_region_prompt(mock_adapter)
    assert "默认" in prompt, f"Expected fallback marker '默认' in prompt, got: {prompt!r}"


def test_build_dynamic_brain_region_prompt_none_result():
    """When adapter.query() returns None, dynamic prompt falls back to defaults."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    mock_adapter = MagicMock()
    mock_adapter.query.return_value = None

    prompt = build_dynamic_brain_region_prompt(mock_adapter)
    assert "默认" in prompt, f"Expected fallback marker '默认' in prompt, got: {prompt!r}"


def test_build_dynamic_brain_region_prompt_whitespace_only():
    """When adapter.query() returns only whitespace, dynamic prompt falls back to defaults."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    mock_adapter = MagicMock()
    mock_adapter.query.return_value = "   \n\t  "

    prompt = build_dynamic_brain_region_prompt(mock_adapter)
    assert "默认" in prompt, f"Expected fallback marker '默认' in prompt, got: {prompt!r}"


def test_build_dynamic_brain_region_prompt_uses_local_mode():
    """Dynamic query uses local mode (no LLM calls)."""
    from niu_api.internal.brain_region_prompt import build_dynamic_brain_region_prompt
    mock_adapter = MagicMock()
    mock_adapter.query.return_value = "brain:region:测试"

    build_dynamic_brain_region_prompt(mock_adapter)

    # Verify query was called with local mode and only_need_context
    mock_adapter.query.assert_called_once()
    call_kwargs = mock_adapter.query.call_args[1]
    assert call_kwargs["mode"] == "local"
    assert call_kwargs["only_need_context"] is True


def test_inject_brain_region_context_adds_to_system_prompt():
    """Injection appends brain region info to the system message."""
    from niu_api.internal.brain_region_prompt import inject_brain_region_context
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities..."},
    ]
    mock_adapter = MagicMock()
    mock_adapter.query.return_value = "brain:region:测试脑区"

    result = inject_brain_region_context(messages, mock_adapter)

    # System message should be modified
    system_msg = next(m for m in result if m["role"] == "system")
    assert "大脑区域架构" in system_msg["content"]
    assert "测试脑区" in system_msg["content"]


def test_inject_brain_region_context_preserves_other_messages():
    """Non-system messages are not modified."""
    from niu_api.internal.brain_region_prompt import inject_brain_region_context
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities..."},
    ]
    mock_adapter = MagicMock()
    mock_adapter.query.return_value = ""

    result = inject_brain_region_context(messages, mock_adapter)

    user_msg = next(m for m in result if m["role"] == "user")
    assert user_msg["content"] == "Extract entities..."


def test_inject_brain_region_context_non_extraction_request_unchanged():
    """Non-extraction requests are not modified at all."""
    from niu_api.internal.brain_region_prompt import inject_brain_region_context
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
    ]
    mock_adapter = MagicMock()

    result = inject_brain_region_context(messages, mock_adapter)

    assert result is messages  # Same object, not a copy
    mock_adapter.query.assert_not_called()


def test_inject_brain_region_context_returns_new_list():
    """Injection returns a new list, does not mutate the original."""
    from niu_api.internal.brain_region_prompt import inject_brain_region_context
    messages = [
        {"role": "system", "content": "---Role---\nYou are a Knowledge Graph Specialist..."},
        {"role": "user", "content": "Extract entities..."},
    ]
    mock_adapter = MagicMock()
    mock_adapter.query.return_value = ""

    result = inject_brain_region_context(messages, mock_adapter)

    assert result is not messages
    # Original system message should NOT contain brain region info
    assert "brain:Niu" not in messages[0]["content"]
