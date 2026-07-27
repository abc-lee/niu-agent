"""apiBase → LiteLLM provider 前缀推导函数单元测试。

验证 _derive_provider_prefix 能从 apiBase 自动推导正确的 provider 前缀，
让 LiteLLM 走对路由（volcengine/openai/anthropic），不依赖用户手选 provider 字段。

通用性：从 apiBase URL 推导 provider 是标准做法（curl/httpx 都这么做），
不是豆包特定 hack。LiteLLM 的 provider 名（volcengine/openai/anthropic）是内置的。
"""
from agent.generic.litellm_adapter import _derive_provider_prefix


# ============================================================================
# 测试用例
# ============================================================================

def test_volcengine_api_base_derives_volcengine_prefix():
    """豆包 ark.cn-beijing.volces.com → volcengine/ 前缀。"""
    assert _derive_provider_prefix("https://ark.cn-beijing.volces.com/api/coding/v3", "ark-code-latest") == "volcengine/ark-code-latest"


def test_anthropic_api_base_derives_anthropic_prefix():
    """api.anthropic.com → anthropic/ 前缀。"""
    assert _derive_provider_prefix("https://api.anthropic.com/v1", "claude-3-5-sonnet") == "anthropic/claude-3-5-sonnet"


def test_openai_api_base_derives_openai_prefix():
    """api.openai.com → openai/ 前缀。"""
    assert _derive_provider_prefix("https://api.openai.com/v1", "gpt-4") == "openai/gpt-4"


def test_xfyun_api_base_derives_openai_prefix():
    """GLM xf-yun 网关 → openai/ 前缀（OpenAI 兼容网关）。"""
    assert _derive_provider_prefix("https://maas-coding-api.cn-huabei-1.xf-yun.com/v2", "xopglm5") == "openai/xopglm5"


def test_custom_gateway_derives_openai_prefix():
    """自定义网关（one-api/new-api）→ openai/ 前缀（默认 OpenAI 兼容）。"""
    assert _derive_provider_prefix("https://my-gateway.example.com/v1", "gpt-4") == "openai/gpt-4"


def test_localhost_derives_openai_prefix():
    """本地 Ollama → openai/ 前缀。"""
    assert _derive_provider_prefix("http://localhost:11434/v1", "llama3") == "openai/llama3"


def test_empty_api_base_derives_openai_prefix():
    """空 apiBase → openai/ 前缀（安全默认）。"""
    assert _derive_provider_prefix("", "gpt-4") == "openai/gpt-4"


def test_none_api_base_derives_openai_prefix():
    """None apiBase → openai/ 前缀（安全默认）。"""
    assert _derive_provider_prefix(None, "gpt-4") == "openai/gpt-4"


def test_case_insensitive_api_base():
    """apiBase 大小写不敏感。"""
    assert _derive_provider_prefix("HTTPS://ARK.CN-BEIJING.VOLCES.COM/api/coding/v3", "ark-code-latest") == "volcengine/ark-code-latest"
