"""Test extra_body 统一送达参数组装（组件 2/3 契约）。

覆盖：
① assemble_request_params 单独调用返回 dict 仅含 extra_body/drop_params 键
   （不含 messages/model/stream 等调用态字段）
② chat() 最终 request_params 基础组装字段保留（stream/tools/response_format/
   api_base/api_key/timeout/extra_headers）
③ 合并语义用户 extra_body 键优先（{**注入, **用户}——用户显式配置胜出、
   注入值不整块丢失）
④ reasoning_effort/thinking 入 extra_body、空配置不注入、litellm_kwargs
   既有机制不破坏
⑤ llmcore max 归一化断言（cfg reasoning_effort="max" → self.reasoning_effort=="max"
   非 None——防 max 被过滤回 None 回归）
⑥ none 生产排除断言（cfg reasoning_effort="none" → 增量不含 reasoning_effort）
"""

from unittest.mock import patch


def _chat_call_kwargs(cfg, *, response_format=None, tools=None):
    """调用 LiteLLMSession.chat（mock litellm.completion 抛异常），返回 request_params。"""
    from agent.generic.litellm_adapter import LiteLLMSession

    session = LiteLLMSession(cfg=cfg)
    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion:
        mock_completion.side_effect = Exception("stop-test")
        try:
            gen = session.chat(
                messages=[{"role": "user", "content": "test"}],
                response_format=response_format,
                tools=tools,
            )
            next(gen)
        except Exception:
            pass
        return mock_completion.call_args[1]


def _base_cfg(**overrides):
    cfg = {
        "api_type": "openai",
        "apikey": "test-key",
        "apibase": "https://api.openai.com/v1",
        "model": "gpt-4o",
    }
    cfg.update(overrides)
    return cfg


# ① assemble_request_params 单独调用：仅含 extra_body/drop_params 键


def test_assemble_request_params_returns_increment_only_keys():
    """增量 dict 仅含 extra_body/drop_params 键，不含调用态字段。"""
    from agent.generic.litellm_adapter import assemble_request_params

    result = assemble_request_params({
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "disabled"}},
    })
    assert set(result.keys()) == {"extra_body", "drop_params"}, (
        f"increment should only carry extra_body/drop_params, got: {list(result.keys())}"
    )
    for forbidden in ("messages", "model", "stream", "tools", "response_format",
                      "api_base", "api_key", "timeout", "max_tokens"):
        assert forbidden not in result, f"increment must not contain call-site field {forbidden!r}"


def test_assemble_request_params_empty_config_returns_empty_dict():
    """空配置（无 reasoning_effort/litellm_kwargs）→ 空增量（无 extra_body/drop_params 键）。"""
    from agent.generic.litellm_adapter import assemble_request_params

    result = assemble_request_params({})
    assert result == {}


def test_assemble_request_params_raw_values_unconditionally_injected():
    """raw_reasoning_effort 非 None（探测）无条件注入——绕过 none 排除与 llmcore 归一化。"""
    from agent.generic.litellm_adapter import assemble_request_params

    result = assemble_request_params(
        {"reasoning_effort": "high", "litellm_kwargs": {}},  # 生产值 high 被 raw 覆盖
        raw_reasoning_effort="none",
        raw_thinking={"type": "enabled"},
    )
    assert set(result.keys()) == {"extra_body", "drop_params"}
    assert result["extra_body"]["reasoning_effort"] == "none"  # raw 无条件注入
    assert result["extra_body"]["thinking"] == {"type": "enabled"}
    assert result["drop_params"] is True  # raw 非 None 触发


# ② chat() 基础组装字段保留


def test_chat_retains_base_assembly_fields():
    """chat() 最终 request_params 保留 stream/tools/response_format/api_base/api_key/
    timeout/extra_headers 等基础组装字段。"""
    tools = [{
        "type": "function",
        "function": {"name": "probe_tool", "description": "d", "parameters": {"type": "object", "properties": {}}},
    }]
    response_format = {"type": "json_object"}
    call_kwargs = _chat_call_kwargs(
        _base_cfg(model="claude-sonnet-4-20250514", reasoning_effort="high"),
        response_format=response_format,
        tools=tools,
    )
    assert call_kwargs["stream"] is True
    assert call_kwargs["stream_options"] == {"include_usage": True}
    assert call_kwargs["api_base"] == "https://api.openai.com/v1"
    assert call_kwargs["api_key"] == "test-key"
    assert call_kwargs["timeout"] == 300  # read_timeout 默认 300
    assert call_kwargs["extra_headers"] == {"anthropic-beta": "prompt-caching-2024-07-31"}
    assert call_kwargs["response_format"] == response_format
    # claude 模型 _convert_tools_schema 会给最后一个 tool 打 cache_control breakpoint——
    # 只断言工具送达（名称/参数保留），不断言逐字节相等
    assert call_kwargs["tools"][0]["function"]["name"] == "probe_tool"
    assert call_kwargs["messages"] == [{"role": "user", "content": "test"}]
    # 注入也送达（claude + high）
    assert call_kwargs["extra_body"]["reasoning_effort"] == "high"


def test_chat_omits_none_base_fields():
    """None 参数不产键：未配置 temperature/proxy 时 request_params 不含对应键。"""
    call_kwargs = _chat_call_kwargs(_base_cfg())
    assert "temperature" not in call_kwargs
    assert "proxy" not in call_kwargs
    assert "proxies" not in call_kwargs


# ③ 合并语义：用户 extra_body 键优先


def test_assemble_merge_user_extra_body_keys_win():
    """合并语义 {**注入, **用户}：用户显式 extra_body 键优先，注入值不整块丢失。"""
    from agent.generic.litellm_adapter import assemble_request_params

    result = assemble_request_params({
        "reasoning_effort": "high",
        "litellm_kwargs": {
            "thinking": {"type": "disabled"},
            "extra_body": {"custom_header": "x", "reasoning_effort": "user-wins"},
        },
    })
    assert result["extra_body"]["reasoning_effort"] == "user-wins", "用户同名键应胜出"
    assert result["extra_body"]["thinking"] == {"type": "disabled"}, "注入值不应整块丢失"
    assert result["extra_body"]["custom_header"] == "x", "用户其他键应保留"
    assert result["drop_params"] is True


def test_chat_merge_user_extra_body_keys_win():
    """chat() 层合并：litellm_kwargs.extra_body 用户键优先，注入值共存。"""
    call_kwargs = _chat_call_kwargs(_base_cfg(
        reasoning_effort="high",
        litellm_kwargs={"extra_body": {"custom_header": "x"}},
    ))
    assert call_kwargs["extra_body"] == {"reasoning_effort": "high", "custom_header": "x"}, (
        f"user extra_body should merge with injected values, got: {call_kwargs['extra_body']}"
    )


# ④ reasoning_effort/thinking 入 extra_body、空配置不注入、litellm_kwargs 既有机制不破坏


def test_reasoning_effort_delivered_via_extra_body():
    """生产 reasoning_effort 进 extra_body 送达（不再进顶层参数）。"""
    call_kwargs = _chat_call_kwargs(_base_cfg(reasoning_effort="high"))
    assert call_kwargs["extra_body"]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in {k for k in call_kwargs if k != "extra_body"}, (
        "reasoning_effort 不应再进顶层参数（消除双通道冗余）"
    )


def test_thinking_delivered_via_extra_body_only():
    """thinking 仅经 extra_body 送达（顶层无 thinking，消除双通道冗余）；drop_params 由
    litellm_kwargs 非空触发（置位逻辑不变）。"""
    call_kwargs = _chat_call_kwargs(_base_cfg(
        litellm_kwargs={"thinking": {"type": "disabled"}},
    ))
    assert call_kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    assert "thinking" not in call_kwargs, "thinking 不应再出现在顶层（统一经 extra_body 送达）"
    assert call_kwargs["drop_params"] is True, "litellm_kwargs 非空应触发 drop_params"


def test_thinking_excluded_from_top_level_other_kwargs_passthrough_kept():
    """新不变式：request_params 顶层无 thinking、extra_body["thinking"] 在场；
    allowed_openai_params 等其余 litellm_kwargs 键仍顶层透传（litellm 当 kwarg 消费）。"""
    call_kwargs = _chat_call_kwargs(_base_cfg(
        litellm_kwargs={
            "thinking": {"type": "enabled"},
            "allowed_openai_params": ["response_format"],
        },
    ))
    assert "thinking" not in call_kwargs, "thinking 仅应存在于 extra_body（唯一送达通道）"
    assert call_kwargs["extra_body"]["thinking"] == {"type": "enabled"}
    assert call_kwargs["allowed_openai_params"] == ["response_format"], "其余键仍顶层透传"
    assert call_kwargs["drop_params"] is True, "litellm_kwargs 非空应触发 drop_params"


def test_empty_config_no_injection():
    """空配置（无 reasoning_effort/litellm_kwargs）→ 不注入 extra_body。"""
    call_kwargs = _chat_call_kwargs(_base_cfg())
    assert "extra_body" not in call_kwargs


def test_litellm_kwargs_passthrough_mechanism_kept():
    """litellm_kwargs 既有透传机制不破坏（max_tokens 等原样 update 进 request_params）。"""
    call_kwargs = _chat_call_kwargs(_base_cfg(
        litellm_kwargs={"max_tokens": 256, "allowed_openai_params": ["response_format"]},
    ))
    assert call_kwargs["max_tokens"] == 256
    assert call_kwargs["allowed_openai_params"] == ["response_format"]
    assert call_kwargs["drop_params"] is True
    # 无注入值 → 不产生 extra_body
    assert "extra_body" not in call_kwargs


# ⑤ llmcore max 归一化


def test_llmcore_normalizes_max_reasoning_effort():
    """llmcore 合法值集合加 max：cfg reasoning_effort="max" 归一化后非 None（防过滤回归）。"""
    from agent.generic.litellm_adapter import LiteLLMSession

    session = LiteLLMSession(cfg=_base_cfg(reasoning_effort="max"))
    assert session.reasoning_effort == "max", (
        f"max 应被 llmcore 归一化保留，got: {session.reasoning_effort!r}"
    )


def test_chat_delivers_max_via_extra_body():
    """生产路径 max 直发：reasoning_effort=max 经 extra_body 送达。"""
    call_kwargs = _chat_call_kwargs(_base_cfg(reasoning_effort="max"))
    assert call_kwargs["extra_body"]["reasoning_effort"] == "max"
    assert "drop_params" not in call_kwargs, "max（生产，无 litellm_kwargs/response_format）不触发 drop_params"


# ⑥ none 生产排除


def test_none_production_excluded_from_extra_body():
    """cfg reasoning_effort="none"（llmcore 合法真值）→ 增量不含 reasoning_effort。

    none 语义由 thinking disabled 表达（豆包/zen none 400 实测），注入层拦截。
    """
    from agent.generic.litellm_adapter import assemble_request_params

    result = assemble_request_params({"reasoning_effort": "none", "litellm_kwargs": {}})
    assert result == {}, f"none 生产应排除（不注入、不触发 drop_params），got: {result}"


def test_none_production_chat_no_extra_body():
    """chat() 生产路径 cfg reasoning_effort="none" → 无 extra_body 注入。"""
    call_kwargs = _chat_call_kwargs(_base_cfg(reasoning_effort="none"))
    assert "extra_body" not in call_kwargs, f"none 不应注入 extra_body，got: {call_kwargs}"
    assert "drop_params" not in call_kwargs
