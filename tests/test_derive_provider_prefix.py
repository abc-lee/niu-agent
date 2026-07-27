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


# ============================================================================
# api_type 参数场景（第三方网关走 anthropic 协议）
# ============================================================================

def test_minimax_anthropic_preset_derives_anthropic_prefix():
    """minimax-anthropic 预设：apiBase=minimaxi.com/anthropic + type=anthropic → anthropic/ 前缀。

    Why: minimax-anthropic 预设用第三方网关（api.minimaxi.com）但走 anthropic 协议，
    apiBase 不含 api.anthropic.com，纯域名匹配会误推成 openai/。需要 api_type=anthropic
    参数覆盖默认推导。
    """
    assert _derive_provider_prefix("https://api.minimaxi.com/anthropic", "MiniMax-M2.7", api_type="anthropic") == "anthropic/MiniMax-M2.7"


def test_minimax_openai_preset_derives_openai_prefix():
    """minimax 预设：apiBase=minimaxi.com/v1 + type=openai → openai/ 前缀（默认）。"""
    assert _derive_provider_prefix("https://api.minimaxi.com/v1", "MiniMax-M2", api_type="openai") == "openai/MiniMax-M2"


def test_anthropic_type_overrides_domain_match():
    """type=anthropic 时，即使 apiBase 不含 anthropic.com，也走 anthropic/ 前缀。"""
    assert _derive_provider_prefix("https://custom-gateway.example.com/anthropic", "claude-3", api_type="anthropic") == "anthropic/claude-3"


def test_volcengine_domain_overrides_anthropic_type():
    """volces.com 域名优先级高于 api_type——豆包走 volcengine 路由，不走 anthropic。"""
    # 豆包不会用 anthropic type，但测试优先级：域名匹配 > api_type
    assert _derive_provider_prefix("https://ark.cn-beijing.volces.com/api/coding/v3", "ark-code-latest", api_type="anthropic") == "volcengine/ark-code-latest"


def test_api_type_none_falls_back_to_domain_match():
    """api_type=None（不传）时，纯按域名匹配（向后兼容）。"""
    assert _derive_provider_prefix("https://ark.cn-beijing.volces.com/api/coding/v3", "ark-code-latest", api_type=None) == "volcengine/ark-code-latest"
    assert _derive_provider_prefix("https://api.minimaxi.com/anthropic", "MiniMax-M2.7", api_type=None) == "openai/MiniMax-M2.7"
