"""Test drop_params semantics after the extra_body 统一送达 refactor.

- reasoning_effort 不再触发 drop_params（extra_body 绕过 litellm 白名单无需丢弃）
- response_format 仍触发 drop_params（调用点置位）
- litellm_kwargs 非空仍触发 drop_params（既有 L866-867 语义保留）
"""

from unittest.mock import patch


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
        assert call_kwargs.get("response_format") == response_format


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
        assert "extra_body" not in call_kwargs, (
            f"extra_body should NOT be injected with empty config, got: {call_kwargs}"
        )


def test_reasoning_effort_not_setting_drop_params():
    """reasoning_effort 只进 extra_body，不再触发 drop_params（原 L858-859 语义删除）。

    extra_body 是 litellm 全路由统一透传通道（不经过白名单），无需 drop_params 丢弃。
    """
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
        assert "drop_params" not in call_kwargs, (
            f"reasoning_effort should NOT trigger drop_params (extra_body bypasses whitelist), got: {call_kwargs}"
        )
        assert call_kwargs["extra_body"]["reasoning_effort"] == "low", (
            f"reasoning_effort should be delivered via extra_body, got: {call_kwargs}"
        )


def test_drop_params_set_when_both_response_format_and_reasoning_effort():
    """reasoning_effort + response_format 同时存在：drop_params 仅由 response_format 触发。

    reasoning_effort 经 extra_body 送达（不触发），response_format 仍是顶层参数（触发）。
    """
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
        assert call_kwargs.get("drop_params") is True, (
            f"drop_params should be True via response_format, got: {call_kwargs.get('drop_params')}"
        )
        assert call_kwargs["extra_body"]["reasoning_effort"] == "low", (
            f"reasoning_effort should still be delivered via extra_body, got: {call_kwargs}"
        )
