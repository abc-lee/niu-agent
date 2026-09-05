"""Sticky routing（会话亲和路由）注入机制测试——spec 2026-09-05-session-sticky-routing-design §3.2/§3.3。

覆盖：
① resolve_sticky_headers 纯函数全用例：域名表正反（含点边界仿冒域 evilopenrouter.ai）、
   大小写变体、scheme-less 两用例、三态语义（auto/off/list 替换）、非法值=off、
   anthropic 排除优先于一切（含列表态）、空列表
② LiteLLMSession.chat() 注入集成：匹配域名带头 / drop_params 下存活 / 探测构造无 id 不发头 /
   控制键剔除 / 用户静态 extra_headers 共存与同键覆盖 / anthropic 不发 / 非匹配域名默认不发 /
   apiBase 翻转跟随实例（AC8）
③ model_probe._build_probe_params 控制键不泄入直发参数
④ create_litellm_client / runner.create_client 白名单透传行
⑤ T2 通道接线：四通道 id 落到实际构造实例（主/子 Agent 同步-异步/LightRAG+脑区 label/MCP sampling）
   + 续答路径 suspended_client 复用（id 与原派发一致）+ AC8 LightRAG 重建幂等

全 mock litellm.completion，禁真实 LLM / 真实 ~/.niu（日志写盘经 patch 短路）。
"""

from unittest.mock import patch


def _resolve(api_base, sticky_config=None, api_type="openai", session_id="main"):
    from agent.generic.litellm_adapter import resolve_sticky_headers
    return resolve_sticky_headers(api_base, sticky_config, api_type, session_id)


# ① resolve_sticky_headers 纯函数：auto 态域名表匹配


def test_auto_openrouter_exact_domain_sends_only_own_header():
    """auto 态只发命中域自家头键——openrouter.ai 不发 x-opencode-session（不交叉互发）。"""
    assert _resolve("https://openrouter.ai/api/v1") == {"x-session-id": "main"}


def test_auto_openrouter_subdomain_hits():
    assert _resolve("https://api.openrouter.ai/v1") == {"x-session-id": "main"}


def test_auto_evilopenrouter_ai_does_not_hit():
    """点边界匹配：字面 endswith 会误中 evilopenrouter.ai。"""
    assert _resolve("https://evilopenrouter.ai/v1") is None


def test_auto_uppercase_domain_variant_hits():
    assert _resolve("https://OPENROUTER.AI/api/v1") == {"x-session-id": "main"}


def test_scheme_less_openrouter_hits():
    """scheme-less apiBase 先补 https:// 再解析（否则 hostname=None 静默失配）。"""
    assert _resolve("openrouter.ai") == {"x-session-id": "main"}


def test_scheme_less_evil_domain_does_not_hit():
    assert _resolve("evilopenrouter.ai") is None


def test_auto_opencode_ai_and_subdomain_hit():
    """opencode.ai 只发自家 x-opencode-session——不发 x-session-id（反向不交叉）。"""
    assert _resolve("https://opencode.ai/v1") == {"x-opencode-session": "main"}
    assert _resolve("https://gateway.opencode.ai/v1") == {"x-opencode-session": "main"}


def test_auto_non_matching_domain_no_headers():
    """火山 ark 等严格网关：auto 缺省不发头（零风险）。"""
    assert _resolve("https://ark.cn-beijing.volces.com/api/v3") is None


def test_auto_trailing_dot_fqdn_hits():
    """尾点 FQDN（'openrouter.ai.'）rstrip('.') 归一后应命中。"""
    assert _resolve("https://openrouter.ai.") == {"x-session-id": "main"}


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
    """列表态只发列表键——该域默认头集不发（opencode 键出现在 openrouter 域 = 纯列表态产物）。"""
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
        "x-opencode-session": "file-processor",
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


def test_chat_matching_domain_injects_own_header_only():
    """openrouter.ai 实例只注入自家 x-session-id（不交叉发 x-opencode-session）。"""
    params = _chat_request_params(_base_cfg(sticky_session_id="main"))
    assert params.get("extra_headers") == {"x-session-id": "main"}


def test_chat_headers_survive_with_drop_params():
    """litellm_kwargs 非空自动开 drop_params=True——注入头仍存活（白名单原生含 extra_headers）。"""
    params = _chat_request_params(_base_cfg(
        sticky_session_id="main",
        litellm_kwargs={"thinking": {"type": "disabled"}},
    ))
    assert params.get("drop_params") is True
    assert params.get("extra_headers") == {"x-session-id": "main"}


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


def test_chat_api_base_flip_between_instances():
    """AC8：注入判定跟随实例自身 api_base——同 id "main" 两实例（ark / openrouter.ai）头集不同；
    配置热重载重建 session 后翻转即时生效（无跨实例全局状态残留）。"""
    params_ark = _chat_request_params(_base_cfg(
        apibase="https://ark.cn-beijing.volces.com/api/v3",
        sticky_session_id="main",
    ))
    assert "extra_headers" not in params_ark

    # 同 id、仅 api_base 翻转（_base_cfg 默认 apibase=openrouter.ai）
    params_or = _chat_request_params(_base_cfg(sticky_session_id="main"))
    assert params_or.get("extra_headers") == {"x-session-id": "main"}


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
    assert "x-opencode-session" not in headers  # openrouter.ai 域不交叉发 opencode 键


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


# ⑤ T2 通道接线：四通道 id 落到实际构造的 LiteLLMSession 实例


def test_runner_init_injects_main_sticky_id():
    """主 Agent：NiuRunner.__init__ 后 client 实例 sticky_session_id=="main"。"""
    from unittest.mock import Mock

    from agent.tool_registry import reset_registry

    try:
        with patch("agent.runner.get_system_prompt", return_value="sys"), \
             patch("agent.runner.get_tools_schema", return_value=[]), \
             patch("agent.runner.get_skill_sync"), \
             patch("agent.runner.NiuHandler"), \
             patch("niu_api.internal.disk_engine.DiskEngine") as mock_disk_cls:
            mock_disk_instance = Mock()
            mock_disk_instance.get_schema.return_value = {
                "type": "function", "function": {"name": "disk"},
            }
            mock_disk_instance.config.servers = {}
            mock_disk_cls.return_value = mock_disk_instance

            from agent.runner import NiuRunner
            runner = NiuRunner(
                llm_config={"apikey": "test", "model": "test-model"},
                mcp_client=None,
            )

        assert runner.client.backend.sticky_session_id == "main"
        assert runner.llm_config["sticky_session_id"] == "main"
    finally:
        # NiuRunner.__init__ 触碰全局 ToolRegistry 单例（get_registry/set_ask_agent）——reset 防泄漏到后续测试
        reset_registry()


def _capture_subagent_llm_config(**call_kwargs):
    """调 call_subagent，在 create_client 处捕获 llm_config 后中断（不跑 agent loop）。"""
    from agent.subagent import call_subagent

    captured = {}

    class _StopTestError(Exception):
        pass

    def fake_create_client(cfg):
        captured.update(cfg)
        raise _StopTestError()

    with patch("agent.subagent.get_subagent_config", return_value={}), \
         patch("agent.subagent.build_subagent_system_segments", return_value=("static", "")), \
         patch("agent.runner.create_client", side_effect=fake_create_client):
        try:
            call_subagent(**call_kwargs)
        except _StopTestError:
            pass
    return captured


def test_subagent_sync_path_uses_agent_name():
    """同步路径（unique_name=None）：id=agent_name，无条件覆盖继承的 "main"。"""
    cfg = _capture_subagent_llm_config(
        agent_name="file-processor", task="t",
        llm_config={"model": "m", "apikey": "k", "sticky_session_id": "main"},
    )
    assert cfg["sticky_session_id"] == "file-processor"


def test_subagent_async_path_uses_unique_name():
    """异步分支（unique_name 非 None + answer=None）：id=unique_name。"""
    cfg = _capture_subagent_llm_config(
        agent_name="file-processor", task="t",
        llm_config={"model": "m", "apikey": "k", "sticky_session_id": "main"},
        unique_name="file-processor-a1b2",
    )
    assert cfg["sticky_session_id"] == "file-processor-a1b2"


def test_subagent_resume_reuses_suspended_client_id():
    """续答路径（answer+answer_unique_name）：复用 suspended_client——sticky_session_id
    与原派发一致（不重建新 client、不按 agent_name 重算 id）。"""
    from unittest.mock import Mock

    from agent.subagent import call_subagent
    from agent.subagent_registry import SubagentRegistry

    unique_name = "file-processor-r3s1"  # 原派发走异步分支：id=unique_name
    suspended_client = Mock()
    suspended_client.backend.sticky_session_id = unique_name
    fresh_client = Mock()
    fresh_client.backend.sticky_session_id = "file-processor"  # 续答新构造（agent_name）——不得被使用

    SubagentRegistry.register(
        agent_type="file-processor", supplement_queue=None, force_unique_name=unique_name,
    )
    inst = SubagentRegistry.get(unique_name)
    inst.state = "waiting_for_answer"
    inst.suspended_messages = [{"role": "user", "content": "task"}]
    inst.suspended_handler = Mock()
    inst.suspended_client = suspended_client
    inst.suspended_tools_schema = []
    inst.suspended_system_message = {"role": "system", "content": "sys"}

    captured = {}

    class _StopTestError(Exception):
        pass

    def fake_loop(client, **kwargs):
        captured["client"] = client
        raise _StopTestError()

    try:
        with patch("agent.subagent.get_subagent_config", return_value={}), \
             patch("agent.subagent.build_subagent_system_segments", return_value=("static", "")), \
             patch("agent.runner.create_client", return_value=fresh_client), \
             patch("agent.subagent._run_agent_loop", side_effect=fake_loop):
            try:
                call_subagent(
                    agent_name="file-processor", task="",
                    llm_config={"model": "m", "apikey": "k", "sticky_session_id": "main"},
                    answer="回答内容", answer_unique_name=unique_name,
                    no_tools=True,
                )
            except _StopTestError:
                pass
        # id 延续由断言链保证：异步派发构造 id=unique_name（test_subagent_async_path_uses_unique_name）
        # + 此处身份复用同一 client——不另加 sticky_session_id 读回相等断言（该值本测试自赋值，同义反复）
        assert captured["client"] is suspended_client
    finally:
        SubagentRegistry._instances.pop(unique_name, None)


def test_lightrag_manager_session_gets_lightrag_id():
    """LightRAG + 脑区：_get_litellm_session 构造实例 id=="lightrag"，无条件覆盖传入值。"""
    from niu_api.internal import lightrag_manager as lm

    try:
        session = lm._get_litellm_session({
            "model": "gpt-4o", "apibase": "https://openrouter.ai/api/v1",
            "apikey": "k", "type": "openai",
            # 传入其他值（脑区 label 复用场景）也必须被覆盖为 "lightrag"
            "sticky_session_id": "brain-label",
        })
        assert session.sticky_session_id == "lightrag"
    finally:
        lm.reset_litellm_session_cache()


def test_region_label_shares_lightrag_session_id():
    """脑区 label：_call_llm_for_label 复用 _get_litellm_session——与 LightRAG 共享同一缓存实例，
    id=="lightrag"（覆盖主配置携带的 "main"）。"""
    from unittest.mock import Mock

    from niu_api.internal import lightrag_manager as lm
    from niu_api.internal.region_manager import RegionManager

    # get_llm_config() 返回值 = 主 Agent 同款模型配置（含主通道 sticky id，须被覆盖）
    main_cfg = {
        "model": "gpt-4o", "apibase": "https://openrouter.ai/api/v1",
        "apikey": "k", "type": "openai",
        "sticky_session_id": "main",
    }
    try:
        with patch("niu_api.llm_proxy.get_llm_config", return_value=main_cfg), \
             patch("agent.generic.litellm_adapter.LiteLLMSession") as mock_session_cls:
            mock_session = Mock()
            mock_session.chat.return_value = iter(["编程脑区"])
            mock_session_cls.return_value = mock_session

            content = RegionManager(adapter=None, ingester=None)._call_llm_for_label(
                "请为该社区生成标签",
            )

        assert content == "编程脑区"
        built_cfg = mock_session_cls.call_args[1]["cfg"]
        assert built_cfg["sticky_session_id"] == "lightrag"
        # 共享缓存判别（构造次数）：label 通道内部一次 + 同配置显式再调一次 = 2 次取用，
        # LiteLLMSession 只构造 1 次——缓存命中不重建（is 断言无判别力：return_value 恒同对象）
        lm._get_litellm_session(main_cfg)
        assert mock_session_cls.call_count == 1
    finally:
        lm.reset_litellm_session_cache()


def test_lightrag_rebuild_restores_sticky_id():
    """AC8 重建幂等：reset_litellm_session_cache 后重建，新实例 sticky_session_id 仍=="lightrag"
    （配置热重载不丢 id）。"""
    from niu_api.internal import lightrag_manager as lm

    cfg = {
        "model": "gpt-4o", "apibase": "https://openrouter.ai/api/v1",
        "apikey": "k", "type": "openai",
    }
    built_cfgs = []

    class _FakeSession:
        def __init__(self, cfg):  # _get_litellm_session 以 LiteLLMSession(cfg=...) 关键字构造
            built_cfgs.append(cfg)

    try:
        with patch("agent.generic.litellm_adapter.LiteLLMSession", _FakeSession):
            s1 = lm._get_litellm_session(cfg)
            lm.reset_litellm_session_cache()
            s2 = lm._get_litellm_session(cfg)

        assert s1 is not s2  # reset 真的触发了重建（未重建则 built_cfgs 只有 1 条，下断言 IndexError）
        assert built_cfgs[0]["sticky_session_id"] == "lightrag"
        assert built_cfgs[1]["sticky_session_id"] == "lightrag"
    finally:
        lm.reset_litellm_session_cache()


def test_mcp_sampling_session_gets_id():
    """MCP Sampling：call_llm_via_litellm 构造实例 id=="mcp-sampling"。"""
    import asyncio

    from niu_api import llm_proxy

    recorded = {}

    class _FakeSession:
        def __init__(self, cfg):
            recorded.update(cfg)

        def chat(self, **kwargs):
            return iter(())

    with patch("agent.generic.litellm_adapter.LiteLLMSession", _FakeSession):
        resp = asyncio.run(llm_proxy.call_llm_via_litellm(
            messages=[{"role": "user", "content": "hi"}],
            config={
                "type": "openai", "apikey": "k",
                "apibase": "https://x.example/v1", "model": "gpt-4o",
                "litellm_kwargs": {},
            },
        ))

    assert recorded["sticky_session_id"] == "mcp-sampling"
    assert resp["choices"][0]["message"]["role"] == "assistant"
