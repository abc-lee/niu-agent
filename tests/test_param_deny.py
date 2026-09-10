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
⑧ T2 参数可用性探测段（D3）+ D2 写侧规则：K3 temperature 400 原文定位 / 无参数名
   回落逐个累积移除 / 提取命中仍 400 落线性 / 连续两参数被拒都进 deny / 全通过只清
   本次测到且通过的 / D5 无法归因不写 / 非 400 fail-closed；frontmatter 0.6 与
   lightrag 默认 0.2 入候选（动机场景回归锁）；response_format 永不入候选/不入 deny；
   写合并语义双向锁（deny↔vision 防互抹）；D2 新建对象 model+probed_at / 串模型守卫 /
   lightrag 回落 llm.model；双落点 user-config + llm-configs 同步；probe() 端到端
   （参数段先于值域扫描）

全 mock litellm.completion，禁真实 LLM / 真实 ~/.niu（日志写盘经 patch 短路）。
"""
import asyncio
import json
from unittest.mock import Mock, patch

import pytest

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


# ---------------------------------------------------------------------------
# 8 T2：参数可用性探测段（D3）+ D2 写侧规则（model_probe.py）
# ---------------------------------------------------------------------------

T2_PROBE_ARGS = dict(
    api_base="https://api.kimi.com/coding/v1",
    api_key="sk-k3",
    model="k3-256k",
    api_type="openai",
)


def _ok_resp():
    from types import SimpleNamespace
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))])


def _bad_400(message, body=None):
    """按 litellm 异常构造 400（e.body 缺失/None 亦合法——R19 同型）。"""
    from litellm import BadRequestError
    return BadRequestError(message, model="probe-model", llm_provider="openai", body=body)


def _auth_401():
    from litellm import AuthenticationError
    return AuthenticationError("401 invalid api key", llm_provider="openai", model="probe-model")


K3_TEMP_400 = (
    "Error code: 400 - {'error': {'message': 'invalid temperature: only 1 is allowed "
    "for this model', 'type': 'invalid_request_error'}}"
)


@pytest.fixture
def probe_paths(tmp_path, monkeypatch):
    """隔离 model_probe 全部写落点（user-config / llm-configs / 档案 → tmp）。"""
    import niu_api.model_probe as mp
    monkeypatch.setattr(mp, "USER_CONFIG_PATH", str(tmp_path / "user-config.json"))
    monkeypatch.setattr(mp, "NAMED_CONFIGS_PATH", str(tmp_path / "llm-configs.json"))
    monkeypatch.setattr(mp, "PROFILE_PATH", tmp_path / "model_capabilities.json")
    return tmp_path


def _patch_frontmatter(monkeypatch, temperature=0.6):
    """frontmatter 温度确定性注入（get_subagent_config("niu")——函数内 import，模块属性 patch 生效）。"""
    monkeypatch.setattr(
        "agent.subagent.get_subagent_config", lambda name: {"temperature": temperature}
    )


def _run_param_section(monkeypatch, probe_paths, section, results, lightrag=False, **probe_overrides):
    """跑 _probe_param_availability（mock completion 按序），返回 (返回值, profile, calls)。"""
    import niu_api.model_probe as mp
    calls = []

    def _side(*a, **kw):
        calls.append(kw)
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    profile = {}
    args = dict(T2_PROBE_ARGS)
    args.update(probe_overrides)
    with patch("niu_api.model_probe.litellm.completion", side_effect=_side):
        ok = mp._probe_param_availability(
            args["api_base"], args["api_key"], args["model"], args["api_type"],
            section, lightrag, profile,
        )
    return ok, profile, calls


def _read_config(tmp_path):
    return json.loads((tmp_path / "user-config.json").read_text(encoding="utf-8"))


class TestParamAvailabilityIdentification:
    """D3 定位算法：错误消息提取（仅作候选）+ 逐个累积移除。"""

    def test_k3_temperature_400_original_message_writes_deny(self, monkeypatch, probe_paths):
        """K3 400 原文 → 正则提取 temperature ∈ 候选 → 移除重试通过 → deny=["temperature"]；
        新建对象含 model+probed_at（D2 R2-B P1-a）。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k"}})
        _patch_frontmatter(monkeypatch)
        ok, profile, calls = _run_param_section(
            monkeypatch, probe_paths, {}, [_bad_400(K3_TEMP_400), _ok_resp()])
        assert ok is True and profile == {}  # 定位成功不毒化主探测
        # wire：首发含 frontmatter 温度 + max_tokens；移除后重试不含 temperature
        assert calls[0]["temperature"] == 0.6
        assert calls[0]["max_tokens"] == 256
        assert "temperature" not in calls[1]
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert caps["deny"] == ["temperature"]
        assert caps["model"] == "k3-256k"  # 新建对象规则：model 绑定（否则 fail-closed 永不通过）
        assert "probed_at" in caps

    def test_no_param_name_in_error_falls_back_to_linear_removal(self, monkeypatch, probe_paths):
        """错误消息无参数名 → 白名单顺序逐个累积移除（每次只移一个、先前保持移除）。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k", "top_p": 0.9}})
        _patch_frontmatter(monkeypatch)
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {"top_p": 0.9},
            [_bad_400("Error code: 400 - bad request"), _bad_400(None), _ok_resp()])
        assert ok is True
        # 累积移除：temperature（白名单序第一）→ top_p → 仅 max_tokens 通过
        assert "temperature" not in calls[1] and calls[1]["top_p"] == 0.9
        assert "top_p" not in calls[2] and "temperature" not in calls[2]
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert caps["deny"] == ["temperature", "top_p"]

    def test_extracted_name_still_400_falls_to_linear(self, monkeypatch, probe_paths):
        """提取命中 temperature 但移除后仍 400（无参数名）→ 回落线性移除 top_p。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k", "top_p": 0.9}})
        _patch_frontmatter(monkeypatch)
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {"top_p": 0.9},
            [_bad_400(K3_TEMP_400), _bad_400(None), _ok_resp()])
        assert ok is True
        assert "temperature" not in calls[1] and calls[1]["top_p"] == 0.9
        assert "top_p" not in calls[2]
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert caps["deny"] == ["temperature", "top_p"]

    def test_two_consecutive_rejections_both_denied(self, monkeypatch, probe_paths):
        """连续两参数被拒（不同错误消息）→ 都进 deny。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k", "top_p": 0.9}})
        _patch_frontmatter(monkeypatch)
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {"top_p": 0.9},
            [_bad_400(K3_TEMP_400),
             _bad_400("Error code: 400 - {'error': {'message': \"'top_p' does not support this model\"}}"),
             _ok_resp()])
        assert ok is True
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert caps["deny"] == ["temperature", "top_p"]

    def test_all_pass_clears_only_tested_and_passed(self, monkeypatch, probe_paths):
        """全通过 → 只清本次实际测到且通过的参数；不在发送集的旧 deny（seed）保留。"""
        _write_config(probe_paths, {
            "llm": {"model": "k3-256k",
                    "capabilities": {"model": "k3-256k", "input": ["text"],
                                     "deny": ["temperature", "seed"]}},
        })
        _patch_frontmatter(monkeypatch)
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {}, [_ok_resp()])
        assert ok is True and len(calls) == 1
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert "deny" not in caps or caps["deny"] == ["seed"], \
            f"只清本次测到且通过的（temperature/max_tokens），seed 保留: {caps}"
        assert caps["input"] == ["text"]  # 合并语义：input 保留

    def test_unattributable_400_after_exhaustion_writes_no_deny(self, monkeypatch, probe_paths):
        """D5：候选清空后仍 400 → 无法归因——不猜、不写 deny、failed 原样报错。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k"}})
        _patch_frontmatter(monkeypatch)
        ok, profile, calls = _run_param_section(
            monkeypatch, probe_paths, {},
            [_bad_400(None), _bad_400(None), _bad_400(None)])
        assert ok is False
        assert profile["probe_status"] == "failed"
        assert "无法定位" in profile["probe_fail_reason"]
        assert len(calls) == 3  # 首发 + 2 次累积移除后清空仍 400 → 停（不无限循环）
        assert "capabilities" not in _read_config(probe_paths)["llm"]  # 未写盘

    def test_non_400_error_fails_closed_no_deny(self, monkeypatch, probe_paths):
        """定位前非 400 错误（401）→ failed fail-closed：不写 deny 保持旧值。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k"}})
        _patch_frontmatter(monkeypatch)
        ok, profile, calls = _run_param_section(
            monkeypatch, probe_paths, {}, [_auth_401()])
        assert ok is False and len(calls) == 1
        assert profile["probe_status"] == "failed"
        assert "capabilities" not in _read_config(probe_paths)["llm"]


class TestParamCandidateSet:
    """D3-1/D3-2 候选集 = 运行时实际发送集 ∩ 白名单。"""

    def test_frontmatter_temperature_in_candidates_llm_scenario(self, monkeypatch, probe_paths):
        """动机场景回归锁：主 llm 段无 temperature 键，探测参数集仍含 niu.md frontmatter 0.6。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k"}})
        _patch_frontmatter(monkeypatch, temperature=0.6)
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {}, [_ok_resp()])
        assert ok is True
        assert calls[0]["temperature"] == 0.6

    def test_frontmatter_overrides_section_temperature_when_both_present(self, monkeypatch, probe_paths):
        """both-present：段显式 temperature + niu.md frontmatter 并存 → 候选取 frontmatter 值
        （镜像 runner.py:767 覆盖序——运行时 frontmatter 无条件覆盖段值，候选集须一致）。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k", "temperature": 0.9}})
        _patch_frontmatter(monkeypatch, temperature=0.6)
        import niu_api.model_probe as mp
        candidates = mp._collect_param_candidates({"temperature": 0.9}, lightrag=False)
        assert candidates["temperature"] == 0.6
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {"temperature": 0.9}, [_ok_resp()])
        assert ok is True
        assert calls[0]["temperature"] == 0.6  # wire：实际发送 frontmatter 值（运行时形态）

    def test_lightrag_default_temperature_02_in_candidates(self, monkeypatch, probe_paths):
        """动机场景回归锁：lightrag_llm 段无 temperature 键，默认 0.2 恒发（lightrag_manager:103）。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k"}, "lightrag_llm": {}})
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {}, [_ok_resp()], lightrag=True)
        assert ok is True
        assert calls[0]["temperature"] == 0.2

    def test_response_format_never_candidate(self, monkeypatch, probe_paths):
        """response_format 排除（R1）：段含该键也不入候选集。"""
        import niu_api.model_probe as mp
        section = {"response_format": {"type": "json_object"}, "top_p": 0.9}
        candidates = mp._collect_param_candidates(section, lightrag=False)
        assert "response_format" not in candidates
        assert set(candidates) == {"temperature", "top_p", "max_tokens"}

    def test_response_format_rejection_never_enters_deny(self, monkeypatch, probe_paths):
        """错误消息提 response_format（不在候选集）→ 不采信，回落线性移除白名单参数；deny 无 rf。"""
        _write_config(probe_paths, {"llm": {"model": "k3-256k"}})
        _patch_frontmatter(monkeypatch)
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {},
            [_bad_400("Error code: 400 - invalid response_format"), _bad_400(None), _ok_resp()])
        assert ok is True
        assert "response_format" not in calls[0]  # wire：rf 从不发送
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert "response_format" not in caps["deny"]
        assert caps["deny"] == ["temperature", "max_tokens"]  # 线性累积移除的结果


class TestDenyVisionWriteMerge:
    """D2/R7 写合并语义双向锁：deny↔vision 两写侧防互抹。"""

    def test_deny_then_vision_write_preserves_deny(self, monkeypatch, probe_paths):
        """先 deny 后 vision：_write_vision_capabilities 整对象重建仍保留既有 deny 键。"""
        import niu_api.model_probe as mp
        _write_config(probe_paths, {
            "llm": {"model": "k3-256k",
                    "capabilities": {"model": "k3-256k", "input": ["text"],
                                     "deny": ["temperature"]}},
        })
        mp._write_vision_capabilities(True, "k3-256k")
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert caps["deny"] == ["temperature"]  # 未被互抹
        assert caps["input"] == ["text", "image"]

    def test_vision_then_deny_write_preserves_input(self, monkeypatch, probe_paths):
        """先 vision 后 deny：参数段写侧只增删 deny 键，input/probed_at/model 保留。"""
        _write_config(probe_paths, {
            "llm": {"model": "k3-256k",
                    "capabilities": {"model": "k3-256k", "input": ["text", "image"],
                                     "probed_at": "2026-09-10T10:00:00"}},
        })
        _patch_frontmatter(monkeypatch)
        ok, _, _ = _run_param_section(
            monkeypatch, probe_paths, {}, [_bad_400(K3_TEMP_400), _ok_resp()])
        assert ok is True
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert caps["deny"] == ["temperature"]
        assert caps["input"] == ["text", "image"]  # 未被互抹
        assert caps["probed_at"] == "2026-09-10T10:00:00"


class TestDenyWriteSideRules:
    """D2 写侧规则：新建对象 / 串模型守卫 / lightrag 回落。"""

    def test_lightrag_new_object_written_to_lightrag_section(self, monkeypatch, probe_paths):
        """lightrag 场景：deny 落 lightrag_llm 段；新建对象含 model+probed_at；
        串模型守卫经 llm.model 回落（lightrag_llm 无独立 model）。"""
        _write_config(probe_paths, {
            "llm": {"model": "k3-256k"},
            "lightrag_llm": {"apiKey": "sk", "apiBase": "https://api.kimi.com/coding/v1"},
        })
        ok, _, calls = _run_param_section(
            monkeypatch, probe_paths, {}, [_bad_400(K3_TEMP_400), _ok_resp()], lightrag=True)
        assert ok is True
        data = _read_config(probe_paths)
        caps = data["lightrag_llm"]["capabilities"]
        assert caps["deny"] == ["temperature"]
        assert caps["model"] == "k3-256k"  # 回落 llm.model 通过守卫 → 记探测 model
        assert "probed_at" in caps
        assert "capabilities" not in data["llm"]  # 不误写主 llm 段

    def test_cross_model_guard_skips_write(self, monkeypatch, probe_paths):
        """串模型守卫（R2-B P1-b）：配置段 model ≠ 探测 model → 跳过写入 + log。"""
        _write_config(probe_paths, {"llm": {"model": "main-model"}})
        _patch_frontmatter(monkeypatch)
        before = (probe_paths / "user-config.json").read_text(encoding="utf-8")
        ok, _, _ = _run_param_section(
            monkeypatch, probe_paths, {}, [_bad_400(K3_TEMP_400), _ok_resp()],
            model="candidate-x")
        assert ok is True  # 探测本身成功（定位也成功），只是不写盘
        after = (probe_paths / "user-config.json").read_text(encoding="utf-8")
        assert before == after, "串模型守卫：配置模型 ≠ 探测模型 → 文件零改动"

    def test_lightrag_guard_fallback_mismatch_skips(self, monkeypatch, probe_paths):
        """lightrag 回落语义的守卫面：llm.model ≠ 探测 model（lightrag_llm 无独立 model）→ 跳过。"""
        _write_config(probe_paths, {
            "llm": {"model": "main-model"},
            "lightrag_llm": {},
        })
        ok, _, _ = _run_param_section(
            monkeypatch, probe_paths, {}, [_bad_400(K3_TEMP_400), _ok_resp()], lightrag=True)
        assert ok is True
        data = _read_config(probe_paths)
        assert "capabilities" not in data["lightrag_llm"]  # 回落 main-model ≠ k3-256k → 跳过


class TestDenyDualLanding:
    """D2 双落点：user-config + llm-configs.json 命名配置快照同步。"""

    def test_deny_synced_to_named_config_snapshot(self, monkeypatch, probe_paths):
        _write_config(probe_paths, {"llm": {"model": "k3-256k", "presetId": "k3-preset"}})
        _patch_frontmatter(monkeypatch)
        ok, _, _ = _run_param_section(
            monkeypatch, probe_paths, {}, [_bad_400(K3_TEMP_400), _ok_resp()])
        assert ok is True
        nc_path = probe_paths / "llm-configs.json"
        assert nc_path.exists(), "presetId 非空 → 命名配置快照必须同步"
        nc = json.loads(nc_path.read_text(encoding="utf-8"))
        snap_caps = nc["configs"]["k3-preset"]["llm"]["capabilities"]
        assert snap_caps["deny"] == ["temperature"]
        assert snap_caps["model"] == "k3-256k"

    def test_unchanged_deny_still_syncs_named_config(self, monkeypatch, probe_paths):
        """P3：new_deny == current 早退仅跳过主配置写盘，命名配置同步照跑（防快照漂移）。"""
        _write_config(probe_paths, {
            "llm": {"model": "k3-256k", "presetId": "k3-preset",
                    "capabilities": {"model": "k3-256k", "deny": ["temperature"]}},
        })
        _patch_frontmatter(monkeypatch)
        before = (probe_paths / "user-config.json").read_text(encoding="utf-8")
        ok, _, _ = _run_param_section(
            monkeypatch, probe_paths, {}, [_bad_400(K3_TEMP_400), _ok_resp()])
        assert ok is True  # temperature 再被拒 → new_deny == current（无变化路径）
        after = (probe_paths / "user-config.json").read_text(encoding="utf-8")
        assert before == after, "deny 无变化 → 主配置文件零改动"
        nc_path = probe_paths / "llm-configs.json"
        assert nc_path.exists(), "deny 无变化时命名配置快照仍须同步"


class TestProbeEndToEnd:
    """probe() 接线：参数可用性段位于值域扫描之前（R2-A P2）。"""

    def test_param_section_runs_before_value_domain_scan(self, monkeypatch, probe_paths):
        _write_config(probe_paths, {"llm": {"model": "k3-256k"}})
        _patch_frontmatter(monkeypatch)
        import niu_api.model_probe as mp
        results = [_bad_400(K3_TEMP_400), _ok_resp()]  # 参数段：首发 400 → 移除 temperature → 通过
        results += [_ok_resp()] * 7                     # 值域扫描（并行 FIFO）
        results.append(_ok_resp())                      # 无效值探针（全 200 判别）
        results += [_ok_resp()] * 2                     # thinking enabled/disabled
        results.append(_ok_resp())                      # vision（"OK" → 红图即证伪短路）
        calls = []

        def _side(*a, **kw):
            calls.append(kw)
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with patch("niu_api.model_probe.litellm.completion", side_effect=_side):
            profile = mp.probe(**T2_PROBE_ARGS, user_config={"llm": {"model": "k3-256k"}})
        assert profile["probe_status"] == "ok"
        # 参数段首发（index 0）：frontmatter 温度在 wire 上、无 extra_body；重试（index 1）已移除 temperature
        assert calls[0]["temperature"] == 0.6
        assert "extra_body" not in calls[0]
        assert "temperature" not in calls[1]  # 定位后 temperature 已从候选移除
        value_domain = calls[2:9]  # 值域扫描 7 值（raw 注入 extra_body.reasoning_effort）
        assert all("reasoning_effort" in c.get("extra_body", {}) for c in value_domain)
        caps = _read_config(probe_paths)["llm"]["capabilities"]
        assert caps["deny"] == ["temperature"]
