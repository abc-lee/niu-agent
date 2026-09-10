"""参数约束 deny 发送过滤测试（plan 2026-09-10-param-deny-mechanism D4，T1）。

覆盖：
① parse_capabilities_deny 纯函数：model 绑定 fail-closed / 畸形结构 → 空集
② chat() 过滤点：顶层参数移除 + extra_body 嵌套同名键清理 + 无 deny 原样发送——
   必经 create_client/create_litellm_client 白名单构造路径（防白名单丢键静默死亡）
③ model 绑定 fail-closed（capabilities.model != 当前 model → 不生效）
④ lightrag_manager 缓存：config_key 含 capabilities → deny 变化触发会话重建
⑤ compat 手工 cfg 注入：来源=落盘段 capabilities 经 model 绑定（_probe_llm wire 级 +
   helper 单元，含 lightrag_llm 无独立 model 回落 llm 段语义）
⑥ subagent 续跑分支：suspended_client.backend.capabilities_deny 与重算值同步
⑦ 七业务点覆盖：各通道构造路径把 deny 交付到 chat() 过滤点（spy；MCP Sampling 死路径注明豁免）

全 mock litellm.completion，禁真实 LLM / 真实 ~/.niu（日志写盘经 patch 短路）。
"""
import asyncio
import json
from unittest.mock import Mock, patch

def _write_config(tmp_path, data):
    p = tmp_path / "user-config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


CAPS_K3 = {"model": "k3-256k", "deny": ["temperature"]}

BASE_CONFIG = {
    "llm": {"model": "main-model", "apiKey": "sk-main", "apiBase": "http://main/v1", "type": "openai"},
}


def _base_cfg(**overrides):
    """create_litellm_client 形态配置（camelCase，主 Agent/子 Agent 同源路径）。"""
    cfg = {
        "apiKey": "sk-test",
        "apiBase": "https://example.com/v1",
        "model": "k3-256k",
        "temperature": 0.6,
    }
    cfg.update(overrides)
    return cfg


def _chat_params_via_whitelist(config):
    """必经 create_litellm_client 白名单构造路径，mock litellm.completion 捕获实际发出的 request_params。"""
    from agent.generic.litellm_adapter import create_litellm_client

    client = create_litellm_client(config)
    session = client.backend
    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion, \
         patch("agent.generic.litellm_adapter._write_raw_log"), \
         patch("agent.generic.litellm_adapter._write_interaction_log"):
        mock_completion.side_effect = Exception("stop-test")
        try:
            gen = session.chat(messages=[{"role": "user", "content": "test"}])
            next(gen)
        except Exception:
            pass
    assert mock_completion.call_args is not None, "litellm.completion 未被调用"
    return mock_completion.call_args[1]


def _chat_params_on(session):
    """对已构造的 session 跑 chat()（mock completion），返回实际发出的 request_params。"""
    with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion, \
         patch("agent.generic.litellm_adapter._write_raw_log"), \
         patch("agent.generic.litellm_adapter._write_interaction_log"):
        mock_completion.side_effect = Exception("stop-test")
        try:
            gen = session.chat(messages=[{"role": "user", "content": "test"}])
            next(gen)
        except Exception:
            pass
    assert mock_completion.call_args is not None
    return mock_completion.call_args[1]


# ---------------------------------------------------------------------------
# ① parse_capabilities_deny 纯函数
# ---------------------------------------------------------------------------

class TestParseCapabilitiesDeny:
    def test_bound_model_returns_deny_set(self):
        from agent.generic.litellm_adapter import parse_capabilities_deny
        assert parse_capabilities_deny(CAPS_K3, "k3-256k") == frozenset({"temperature"})

    def test_model_mismatch_fail_closed(self):
        """capabilities.model != 当前 model（换模型后旧判定）→ 空集。"""
        from agent.generic.litellm_adapter import parse_capabilities_deny
        assert parse_capabilities_deny(CAPS_K3, "other-model") == frozenset()

    def test_missing_caps_model_key_fail_closed(self):
        """capabilities 无 model 键 → 空集（防无绑定对象误生效）。"""
        from agent.generic.litellm_adapter import parse_capabilities_deny
        assert parse_capabilities_deny({"deny": ["temperature"]}, "k3-256k") == frozenset()

    def test_empty_current_model_fail_closed(self):
        from agent.generic.litellm_adapter import parse_capabilities_deny
        assert parse_capabilities_deny(CAPS_K3, "") == frozenset()
        assert parse_capabilities_deny(None, "k3-256k") == frozenset()

    def test_malformed_deny_not_list(self):
        from agent.generic.litellm_adapter import parse_capabilities_deny
        caps = {"model": "k3-256k", "deny": "temperature"}  # 标量非列表
        assert parse_capabilities_deny(caps, "k3-256k") == frozenset()

    def test_deny_non_string_items_dropped(self):
        from agent.generic.litellm_adapter import parse_capabilities_deny
        caps = {"model": "k3-256k", "deny": [123, "", "temperature"]}
        assert parse_capabilities_deny(caps, "k3-256k") == frozenset({"temperature"})

    def test_multiple_deny_keys(self):
        from agent.generic.litellm_adapter import parse_capabilities_deny
        caps = {"model": "k3-256k", "deny": ["temperature", "top_p"]}
        assert parse_capabilities_deny(caps, "k3-256k") == frozenset({"temperature", "top_p"})


# ---------------------------------------------------------------------------
# ② chat() 过滤点（必经白名单构造路径）
# ---------------------------------------------------------------------------

class TestChatDenyFilter:
    def test_deny_removes_top_level_temperature(self):
        """deny=["temperature"] → wire 体无 temperature（模型用自身默认值）。"""
        params = _chat_params_via_whitelist(_base_cfg(capabilities=CAPS_K3))
        assert "temperature" not in params
        # 其余参数不受影响
        assert params["model"].endswith("k3-256k")
        assert params["stream"] is True

    def test_deny_cleans_nested_extra_body_key(self):
        """deny 键经 litellm_kwargs.extra_body 注入 → 顶层与嵌套同名键同步清理。"""
        params = _chat_params_via_whitelist(_base_cfg(
            capabilities=CAPS_K3,
            litellm_kwargs={"extra_body": {"temperature": 0.9}},
        ))
        assert "temperature" not in params
        assert "temperature" not in (params.get("extra_body") or {})

    def test_no_deny_sends_unchanged(self):
        """无 capabilities → 原样发送（零行为变化）。"""
        params = _chat_params_via_whitelist(_base_cfg())
        assert params["temperature"] == 0.6

    def test_model_mismatch_does_not_filter(self):
        """③ model 绑定 fail-closed：capabilities.model != 当前 model → deny 不生效。"""
        params = _chat_params_via_whitelist(_base_cfg(
            capabilities={"model": "other-model", "deny": ["temperature"]}))
        assert params["temperature"] == 0.6


# ---------------------------------------------------------------------------
# ③ 白名单构造路径透传（防丢键静默死亡——R5 has_vision 同类教训）
# ---------------------------------------------------------------------------

class TestWhitelistPassthrough:
    def test_create_litellm_client_delivers_deny(self):
        from agent.generic.litellm_adapter import create_litellm_client
        client = create_litellm_client(_base_cfg(capabilities=CAPS_K3))
        assert client.backend.capabilities_deny == frozenset({"temperature"})

    def test_runner_create_client_delivers_deny(self):
        """runner.create_client（主 Agent 构造入口）同型透传。"""
        from agent.runner import create_client
        client = create_client({
            "apikey": "sk-test", "apibase": "https://example.com/v1",
            "model": "k3-256k", "temperature": 0.6,
            "capabilities": CAPS_K3,
        })
        assert client.backend.capabilities_deny == frozenset({"temperature"})

    def test_absent_capabilities_fail_closed_empty(self):
        from agent.generic.litellm_adapter import create_litellm_client
        client = create_litellm_client(_base_cfg())
        assert client.backend.capabilities_deny == frozenset()


# ---------------------------------------------------------------------------
# ④ lightrag_manager 缓存：deny 变化触发会话重建
# ---------------------------------------------------------------------------

class TestLightragCacheRebuild:
    def _config(self, capabilities=None):
        cfg = {
            "model": "k3-256k", "apibase": "http://x/v1", "apikey": "sk",
            "type": "openai", "temperature": 0.2,
        }
        if capabilities is not None:
            cfg["capabilities"] = capabilities
        return cfg

    def test_deny_change_triggers_rebuild(self, monkeypatch):
        import niu_api.internal.lightrag_manager as lm

        monkeypatch.setattr(lm, "_cached_session", None)
        monkeypatch.setattr(lm, "_cached_config_key", None)
        try:
            s1 = lm._get_litellm_session(self._config())
            assert s1.capabilities_deny == frozenset()

            # deny 写入（探测落盘）→ config_key 变化 → 重建
            s2 = lm._get_litellm_session(self._config(CAPS_K3))
            assert s2 is not s1, "deny 变化必须触发会话重建（验收④：不重启即生效）"
            assert s2.capabilities_deny == frozenset({"temperature"})

            # 同配置幂等：不重复重建
            assert lm._get_litellm_session(self._config(CAPS_K3)) is s2

            # deny 清空（洗白）→ 再次重建回无 deny 会话
            s4 = lm._get_litellm_session(self._config())
            assert s4 is not s2
            assert s4.capabilities_deny == frozenset()
        finally:
            lm.reset_litellm_session_cache()


# ---------------------------------------------------------------------------
# ⑤ compat 手工 cfg 注入（来源=落盘段 capabilities 经 model 绑定）
# ---------------------------------------------------------------------------

class TestCompatInjection:
    def test_helper_injects_bound_caps_from_llm_section(self, monkeypatch, tmp_path):
        from niu_api import compat
        data = {
            "llm": {
                **BASE_CONFIG["llm"],
                "capabilities": {"model": "main-model", "deny": ["temperature"]},
            },
        }
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, data))
        cfg = {"model": "main-model"}
        compat._inject_persisted_capabilities(cfg, section="llm")
        assert cfg["capabilities"]["deny"] == ["temperature"]

    def test_helper_model_mismatch_not_injected(self, monkeypatch, tmp_path):
        """落盘段 capabilities.model != 表单 model（测候选模型）→ 不注入。"""
        from niu_api import compat
        data = {
            "llm": {
                **BASE_CONFIG["llm"],
                "capabilities": {"model": "main-model", "deny": ["temperature"]},
            },
        }
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, data))
        cfg = {"model": "candidate-model"}
        compat._inject_persisted_capabilities(cfg, section="llm")
        assert "capabilities" not in cfg

    def test_helper_missing_caps_not_injected(self, monkeypatch, tmp_path):
        from niu_api import compat
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, BASE_CONFIG))
        cfg = {"model": "main-model"}
        compat._inject_persisted_capabilities(cfg, section="llm")
        assert "capabilities" not in cfg

    def test_helper_lightrag_fallback_reads_llm_section(self, monkeypatch, tmp_path):
        """lightrag_llm 段无独立 model → 有效 model 来自主 llm（get_llm_config 继承语义）→ 对应段是 llm。"""
        from niu_api import compat
        data = {
            "llm": {
                **BASE_CONFIG["llm"],
                "capabilities": {"model": "main-model", "deny": ["temperature"]},
            },
            "lightrag_llm": {},  # 空段 → 回落主 llm
        }
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, data))
        cfg = {"model": "main-model"}
        compat._inject_persisted_capabilities(cfg, section="lightrag_llm")
        assert cfg["capabilities"]["deny"] == ["temperature"]

    def test_probe_llm_wire_deny_filters_reasoning_effort(self, monkeypatch, tmp_path):
        """wire 级：落盘 deny=["reasoning_effort"] → _probe_llm 实际请求 extra_body 无该键。"""
        from niu_api.compat import _probe_llm
        data = {
            "llm": {
                **BASE_CONFIG["llm"],
                "capabilities": {"model": "main-model", "deny": ["reasoning_effort"]},
            },
        }
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, data))
        cfg = {
            "apiKey": "sk-main", "apiBase": "http://main/v1",
            "model": "main-model", "type": "openai",
            "reasoning_effort": "high",
        }
        with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion, \
             patch("agent.generic.litellm_adapter._write_raw_log"), \
             patch("agent.generic.litellm_adapter._write_interaction_log"):
            mock_completion.side_effect = Exception("stop-test")
            asyncio.run(_probe_llm(cfg))
        kwargs = mock_completion.call_args[1]
        assert "reasoning_effort" not in (kwargs.get("extra_body") or {}), \
            "deny 在列参数必须被 chat() 过滤（含 extra_body 嵌套）"

    def test_probe_llm_no_persisted_caps_sends_unchanged(self, monkeypatch, tmp_path):
        """对照：无落盘 capabilities → reasoning_effort 原样进 extra_body（零行为变化）。"""
        from niu_api.compat import _probe_llm
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, BASE_CONFIG))
        cfg = {
            "apiKey": "sk-main", "apiBase": "http://main/v1",
            "model": "main-model", "type": "openai",
            "reasoning_effort": "high",
        }
        with patch("agent.generic.litellm_adapter.litellm.completion") as mock_completion, \
             patch("agent.generic.litellm_adapter._write_raw_log"), \
             patch("agent.generic.litellm_adapter._write_interaction_log"):
            mock_completion.side_effect = Exception("stop-test")
            asyncio.run(_probe_llm(cfg))
        kwargs = mock_completion.call_args[1]
        assert (kwargs.get("extra_body") or {}).get("reasoning_effort") == "high"


# ---------------------------------------------------------------------------
# ⑥ subagent 续跑分支：suspended_client.backend.capabilities_deny 与重算值同步
# ---------------------------------------------------------------------------

def test_subagent_resume_syncs_suspended_deny(monkeypatch, tmp_path):
    """挂起档 backend.capabilities_deny=旧值；续答时 llm_config capabilities 匹配 → 覆盖为重算值。"""
    import agent.runner as runner_mod
    from agent import subagent
    from agent.subagent_registry import SubagentRegistry

    class _FakeBackend:
        capabilities_deny = frozenset({"top_p"})  # 派发时刻旧值（当时 deny 不同）

    suspended_client = Mock()
    suspended_client.backend = _FakeBackend()

    monkeypatch.setattr(subagent, "_run_agent_loop", lambda *a, **k: ("done", {"result": "ok"}, ""))
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: Mock())
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, BASE_CONFIG))
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "_maybe_push_subagent_instruction", lambda *a, **k: None)
    monkeypatch.setattr(subagent, "_maybe_suspend_session", lambda *a, **k: None)

    unique_name = SubagentRegistry.register(
        "screenshot-analyst", supplement_queue=Mock(), force_unique_name="sa-deny-resume")
    try:
        inst = SubagentRegistry.get(unique_name)
        assert inst is not None
        inst.state = "waiting_for_answer"
        inst.suspended_messages = [{"role": "user", "content": "task"}]
        inst.suspended_handler = Mock()
        inst.suspended_client = suspended_client
        inst.suspended_tools_schema = []
        inst.suspended_system_message = {"role": "system", "content": "sys"}

        subagent.call_subagent(
            agent_name="screenshot-analyst",
            task="",
            answer="[回答] 继续",
            answer_unique_name=unique_name,
            llm_config={
                "apikey": "sk-main", "apibase": "http://main/v1", "model": "main-model",
                "capabilities": {"model": "main-model", "deny": ["temperature"]},
            },
        )
        assert suspended_client.backend.capabilities_deny == frozenset({"temperature"}), \
            "续跑必须同步重算的 capabilities_deny 到 suspended_client（R8）"
    finally:
        SubagentRegistry.unregister(unique_name)


# ---------------------------------------------------------------------------
# ⑦ 七业务点覆盖：各通道构造路径把 deny 交付到 chat() 过滤点（spy）
# ---------------------------------------------------------------------------

class TestSevenChannelsCoverage:
    """plan §3 七业务出网点全部经 LiteLLMSession.chat()——逐通道验证构造路径
    把 capabilities 交付到会话（过滤点在 chat() 单点，②已锁行为）。
    #6 testAndSave 无独立用例：wire 级覆盖见 TestCompatInjection.test_probe_llm_wire_deny_filters_reasoning_effort。"""

    def _subagent_style_cfg(self):
        # get_llm_config 形态（小写键）——子 Agent 派发链 llm_config 同源
        return {
            "apikey": "sk", "apibase": "http://x/v1", "model": "k3-256k",
            "temperature": 0.3, "capabilities": CAPS_K3,
        }

    def test_1_main_agent_channel(self):
        """#1 主 Agent 工具循环：runner.create_client → chat() 过滤。"""
        from agent.runner import create_client
        client = create_client({
            "apikey": "sk", "apibase": "http://x/v1", "model": "k3-256k",
            "temperature": 0.6, "capabilities": CAPS_K3,
        })
        params = _chat_params_on(client.backend)
        assert "temperature" not in params

    def test_2_subagent_channel(self):
        """#2 子 Agent 循环：同 create_client 路径（get_llm_config 小写键形态）。"""
        from agent.runner import create_client
        client = create_client(self._subagent_style_cfg())
        params = _chat_params_on(client.backend)
        assert "temperature" not in params

    def test_3_4_lightrag_and_brain_region_shared_cache(self, monkeypatch):
        """#3 LightRAG 文件入库 / #4 脑区 label：同 _get_litellm_session 缓存——
        deny 交付 + chat() 过滤 + 两业务点共享同一会话对象（单过滤点）。"""
        import niu_api.internal.lightrag_manager as lm

        monkeypatch.setattr(lm, "_cached_session", None)
        monkeypatch.setattr(lm, "_cached_config_key", None)
        try:
            config = {
                "model": "k3-256k", "apibase": "http://x/v1", "apikey": "sk",
                "type": "openai", "temperature": 0.2, "capabilities": CAPS_K3,
            }
            s_file = lm._get_litellm_session(config)          # 文件入库
            s_region = lm._get_litellm_session(config)        # 脑区 label（同缓存）
            assert s_region is s_file, "两业务点必须共享同一缓存会话（过滤点单点）"
            params = _chat_params_on(s_file)
            assert "temperature" not in params
        finally:
            lm.reset_litellm_session_cache()

    def test_5_mcp_sampling_channel_delivers_key(self):
        """#5 MCP Sampling：llm_config 构造含 capabilities（现状死路径豁免 wire 断言——R1-A P3-8）。"""
        from niu_api import llm_proxy

        recorded = {}

        class _FakeSession:
            def __init__(self, cfg):
                recorded["cfg"] = cfg

            def chat(self, **kw):
                raise RuntimeError("stop-test")

        with patch("agent.generic.litellm_adapter.LiteLLMSession", _FakeSession):
            try:
                asyncio.run(llm_proxy.call_llm_via_litellm(
                    [{"role": "user", "content": "hi"}],
                    config={
                        "apikey": "sk", "apibase": "http://x/v1", "model": "k3-256k",
                        "type": "openai", "capabilities": CAPS_K3,
                    },
                ))
            except Exception:
                pass  # 假会话抛错 → HTTPException——只关心构造交付
        assert recorded.get("cfg", {}).get("capabilities") == CAPS_K3

    def test_7_rf_probe_channel(self, monkeypatch, tmp_path):
        """#7 rf 档位探测：base_llm_config 注入落盘 lightrag_llm 段 capabilities（helper 单元已锁语义，
        此处锁端点调用点接线——section=lightrag_llm）。"""
        from niu_api import compat

        data = {
            "llm": dict(BASE_CONFIG["llm"]),
            "lightrag_llm": {
                "model": "k3-256k", "apiKey": "sk", "apiBase": "http://x/v1",
                "capabilities": CAPS_K3,
            },
        }
        monkeypatch.setattr("niu_api.config.CONFIG_PATH", _write_config(tmp_path, data))
        cfg = {"model": "k3-256k"}
        compat._inject_persisted_capabilities(cfg, section="lightrag_llm")
        assert cfg["capabilities"] == CAPS_K3
