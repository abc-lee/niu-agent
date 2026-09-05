"""Sticky routing（会话亲和路由）注入机制测试——spec 2026-09-05-session-sticky-routing-design §3.2/§3.3。

覆盖：
① resolve_sticky_headers 纯函数全用例：域名表正反（含点边界仿冒域 evilopenrouter.ai）、
   大小写变体、scheme-less 两用例、三态语义（auto/off/list 替换）、非法值=off、
   anthropic 排除优先于一切（含列表态）、空列表
② LiteLLMSession.chat() 注入集成：匹配域名带头 / drop_params 下存活 / 探测构造无 id 不发头 /
   控制键剔除 / 用户静态 extra_headers 共存与同键覆盖 / anthropic 不发 / 非匹配域名默认不发
③ model_probe._build_probe_params 控制键不泄入直发参数
④ create_litellm_client / runner.create_client 白名单透传行

全 mock litellm.completion，禁真实 LLM / 真实 ~/.niu（日志写盘经 patch 短路）。
"""

from unittest.mock import patch


def _resolve(api_base, sticky_config=None, api_type="openai", session_id="main"):
    from agent.generic.litellm_adapter import resolve_sticky_headers
    return resolve_sticky_headers(api_base, sticky_config, api_type, session_id)


# ① resolve_sticky_headers 纯函数：auto 态域名表匹配


def test_auto_openrouter_exact_domain_sends_both_headers():
    assert _resolve("https://openrouter.ai/api/v1") == {
        "x-session-id": "main", "x-opencode-session": "main",
    }


def test_auto_openrouter_subdomain_hits():
    assert _resolve("https://api.openrouter.ai/v1") is not None


def test_auto_evilopenrouter_ai_does_not_hit():
    """点边界匹配：字面 endswith 会误中 evilopenrouter.ai。"""
    assert _resolve("https://evilopenrouter.ai/v1") is None


def test_auto_uppercase_domain_variant_hits():
    assert _resolve("https://OPENROUTER.AI/api/v1") == {
        "x-session-id": "main", "x-opencode-session": "main",
    }


def test_scheme_less_openrouter_hits():
    """scheme-less apiBase 先补 https:// 再解析（否则 hostname=None 静默失配）。"""
    assert _resolve("openrouter.ai") == {
        "x-session-id": "main", "x-opencode-session": "main",
    }


def test_scheme_less_evil_domain_does_not_hit():
    assert _resolve("evilopenrouter.ai") is None


def test_auto_opencode_ai_and_subdomain_hit():
    assert _resolve("https://opencode.ai/v1") == {
        "x-session-id": "main", "x-opencode-session": "main",
    }
    assert _resolve("https://gateway.opencode.ai/v1") is not None


def test_auto_non_matching_domain_no_headers():
    """火山 ark 等严格网关：auto 缺省不发头（零风险）。"""
    assert _resolve("https://ark.cn-beijing.volces.com/api/v3") is None


def test_auto_trailing_dot_fqdn_hits():
    """尾点 FQDN（'openrouter.ai.'）rstrip('.') 归一后应命中。"""
    assert _resolve("https://openrouter.ai.") == {
        "x-session-id": "main", "x-opencode-session": "main",
    }


def test_auto_trailing_dot_evil_domain_still_does_not_hit():
    """尾点归一不得破坏点边界：'evilopenrouter.ai.' 仍不命中。"""
    assert _resolve("https://evilopenrouter.ai.") is None


def test_auto_empty_or_none_api_base_no_headers():
    assert _resolve("") is None
    assert _resolve(None) is None


# ① resolve_sticky_headers：三态 + 非法值 + anthropic 优先级


def test_off_disables_even_on_matching_domain():
    assert _resolve("https://openrouter.ai/api/v1", "off") is None


def test_list_replaces_header_set_unconditionally():
    """列表态替换默认头集、无条件发送（反代场景）；未列入键缺席。"""
    assert _resolve("https://ark.cn-beijing.volces.com/api/v3", ["x-session-id"]) == {
        "x-session-id": "main",
    }


def test_list_state_does_not_send_default_headers():
    """列表态只发列表键——默认双头不发。"""
    headers = _resolve("https://openrouter.ai/api/v1", ["x-opencode-session"])
    assert headers == {"x-opencode-session": "main"}


def test_empty_list_is_off():
    assert _resolve("https://openrouter.ai/api/v1", []) is None


def test_invalid_scalar_values_are_off():
    assert _resolve("https://openrouter.ai/api/v1", "banana") is None
    assert _resolve("https://openrouter.ai/api/v1", 42) is None


def test_anthropic_excluded_before_everything():
    """anthropic 排除优先于一切（含列表态）。"""
    assert _resolve("https://openrouter.ai/api/v1", api_type="anthropic") is None
    assert _resolve(
        "https://ark.cn-beijing.volces.com/api/v3", ["x-session-id"], api_type="anthropic",
    ) is None


def test_custom_session_id_value_used():
    assert _resolve("https://opencode.ai/v1", session_id="file-processor") == {
        "x-session-id": "file-processor", "x-opencode-session": "file-processor",
    }


# ② LiteLLMSession.chat() 注入集成


def _base_cfg(**overrides):
    cfg = {
        "api_type": "openai",
        "apikey": "test-key",
        "apibase": "https://openrouter.ai/api/v1",
        "model": "gpt-4o",
    }
    cfg.update(overrides)
    return cfg


def _chat_request_params(cfg):
    """调用 LiteLLMSession.chat（mock litellm.completion 抛异常），返回 request_params。

    日志写盘 patch 短路——禁触真实 ~/.niu。
    """
    from agent.generic.litellm_adapter import LiteLLMSession

    session = LiteLLMSession(cfg=cfg)
    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion, \
         patch("agent.generic.litellm_adapter._write_raw_log"), \
         patch("agent.generic.litellm_adapter._write_interaction_log"):
        mock_completion.side_effect = Exception("stop-test")
        try:
            gen = session.chat(messages=[{"role": "user", "content": "test"}])
            next(gen)
        except Exception:
            pass
        return mock_completion.call_args[1]


def test_chat_matching_domain_injects_both_headers():
    params = _chat_request_params(_base_cfg(sticky_session_id="main"))
    assert params.get("extra_headers") == {
        "x-session-id": "main", "x-opencode-session": "main",
    }


def test_chat_headers_survive_with_drop_params():
    """litellm_kwargs 非空自动开 drop_params=True——注入头仍存活（白名单原生含 extra_headers）。"""
    params = _chat_request_params(_base_cfg(
        sticky_session_id="main",
        litellm_kwargs={"thinking": {"type": "disabled"}},
    ))
    assert params.get("drop_params") is True
    assert params.get("extra_headers") == {
        "x-session-id": "main", "x-opencode-session": "main",
    }


def test_chat_probe_construction_without_id_sends_no_headers():
    """探测/未接线通道（sticky_session_id 缺省 None）+ 匹配域名 → 不发头（无 None 值头）。"""
    params = _chat_request_params(_base_cfg())
    assert "extra_headers" not in params


def test_chat_control_key_stripped_and_off_respected():
    """控制键 sticky_session_headers 不泄入请求参数；off 态匹配域名也不发头。"""
    params = _chat_request_params(_base_cfg(
        sticky_session_id="main",
        litellm_kwargs={"sticky_session_headers": "off"},
    ))
    assert "sticky_session_headers" not in params
    assert "extra_headers" not in params


def test_chat_list_override_on_non_matching_domain():
    """列表态替换集：非匹配域名（mock/反代）强制启用，只发列表键。"""
    params = _chat_request_params(_base_cfg(
        apibase="https://127.0.0.1:9/v1",
        sticky_session_id="main",
        litellm_kwargs={"sticky_session_headers": ["x-session-id"]},
    ))
    assert params.get("extra_headers") == {"x-session-id": "main"}


def test_chat_user_static_extra_headers_coexist_same_key_overridden():
    """用户自定义键保留；同键静态值被程序动态值覆盖（{**user, **program}）。"""
    params = _chat_request_params(_base_cfg(
        sticky_session_id="main",
        litellm_kwargs={"extra_headers": {"x-custom": "abc", "x-session-id": "static"}},
    ))
    assert params.get("extra_headers") == {
        "x-custom": "abc",
        "x-session-id": "main",
        "x-opencode-session": "main",
    }


def test_chat_anthropic_type_sends_no_headers():
    params = _chat_request_params(_base_cfg(
        api_type="anthropic",
        apibase="https://openrouter.ai/api/v1",
        sticky_session_id="main",
    ))
    assert "extra_headers" not in params


def test_chat_non_matching_domain_auto_sends_no_headers():
    params = _chat_request_params(_base_cfg(
        apibase="https://ark.cn-beijing.volces.com/api/v3",
        sticky_session_id="main",
    ))
    assert "extra_headers" not in params


def test_chat_anthropic_beta_and_sticky_headers_coexist():
    """AC7：openai 兼容端点 + claude 模型名——get_provider_params 预置的 anthropic-beta 头
    随 provider_params 流入 build_base_params，与 sticky 注入头共存且互不覆盖。"""
    params = _chat_request_params(_base_cfg(
        model="claude-3-5-sonnet",
        sticky_session_id="main",
    ))
    headers = params.get("extra_headers") or {}
    assert headers.get("anthropic-beta") == "prompt-caching-2024-07-31"
    assert headers.get("x-session-id") == "main"
    assert headers.get("x-opencode-session") == "main"


# ③ model_probe._build_probe_params：控制键不泄入直发参数


def test_probe_params_strip_sticky_control_key():
    from niu_api.model_probe import _build_probe_params

    probe_config = {
        "litellm_kwargs": {
            "sticky_session_headers": "off",
            "allowed_openai_params": ["thinking"],
        },
    }
    params = _build_probe_params(
        api_base="https://openrouter.ai/api/v1",
        api_key="k",
        model="gpt-4o",
        api_type="openai",
        probe_config=probe_config,
    )
    assert "sticky_session_headers" not in params
    assert params.get("allowed_openai_params") == ["thinking"]
    assert params.get("drop_params") is True


# ④ 白名单构造透传行


def test_create_litellm_client_passthrough():
    from agent.generic.litellm_adapter import create_litellm_client

    client = create_litellm_client({
        "apikey": "k", "apiBase": "https://openrouter.ai/api/v1",
        "model": "gpt-4o", "sticky_session_id": "main",
    })
    assert client.backend.sticky_session_id == "main"

    client_no_id = create_litellm_client({
        "apikey": "k", "apiBase": "https://openrouter.ai/api/v1", "model": "gpt-4o",
    })
    assert client_no_id.backend.sticky_session_id is None


def test_runner_create_client_passthrough():
    from agent.runner import create_client

    client = create_client({
        "apikey": "k", "apibase": "https://openrouter.ai/api/v1",
        "model": "gpt-4o", "type": "openai", "sticky_session_id": "lightrag",
    })
    assert client.backend.sticky_session_id == "lightrag"
