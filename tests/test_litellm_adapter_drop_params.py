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
