# tests/test_max_tokens_passthrough.py
"""max_tokens 用户配置透传测试。"""
from agent.generic.litellm_adapter import LiteLLMSession, create_litellm_client
from agent.runner import create_client


def _base_cfg(**kw):
    cfg = {"apikey": "k", "apibase": "http://x", "model": "m", "api_type": "openai"}
    cfg.update(kw)
    return cfg


class TestSessionMerge:
    def test_top_level_max_tokens_merged_into_kwargs(self):
        s = LiteLLMSession(cfg=_base_cfg(max_tokens=8192))
        assert s.litellm_kwargs["max_tokens"] == 8192

    def test_kwargs_max_tokens_wins_over_top_level(self):
        """kwargs 优先——保护压缩/探测程序注入值不被用户顶层配置覆盖。"""
        s = LiteLLMSession(cfg=_base_cfg(max_tokens=8192, litellm_kwargs={"max_tokens": 256}))
        assert s.litellm_kwargs["max_tokens"] == 256

    def test_no_max_tokens_no_key(self):
        """缺省不传——kwargs 不产 max_tokens 键。"""
        s = LiteLLMSession(cfg=_base_cfg())
        assert "max_tokens" not in s.litellm_kwargs

    def test_existing_kwargs_preserved(self):
        s = LiteLLMSession(cfg=_base_cfg(max_tokens=8192, litellm_kwargs={"thinking": {"type": "enabled"}}))
        assert s.litellm_kwargs["thinking"] == {"type": "enabled"}
        assert s.litellm_kwargs["max_tokens"] == 8192


class TestWhitelistFactories:
    def test_runner_create_client_passes_max_tokens(self):
        client = create_client(_base_cfg(max_tokens=8192))
        assert client.backend.litellm_kwargs["max_tokens"] == 8192

    def test_adapter_factory_passes_max_tokens(self):
        client = create_litellm_client({"apiKey": "k", "apiBase": "http://x", "model": "m", "max_tokens": 8192})
        assert client.backend.litellm_kwargs["max_tokens"] == 8192

    def test_factories_omit_when_absent(self):
        client = create_client(_base_cfg())
        assert "max_tokens" not in client.backend.litellm_kwargs
