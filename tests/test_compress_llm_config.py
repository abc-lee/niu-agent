"""压缩专用 LLM 配置测试：按知识图谱（lightrag 段）用户配置原样透传 + 只注入 max_tokens。

2026-08-18 用户拍板：程序不预设过滤——thinking/effort 按用户 lightrag 段配置原样透传，
不再注入 thinking disabled、不再置 reasoning_effort（组合可用性由 testAndSave 把关）。
mock 目标：niu_api.llm_proxy.get_llm_config（builder 内部局部 import，mock compat 绑定不拦截）。
"""
from unittest.mock import patch

from niu_api.compat import _build_compress_llm_config  # noqa: E402

LIGHTRAG_CFG = {
    "model": "lightrag-model",
    "apikey": "test-key",
    "apibase": "https://example.com/v1",
    "reasoning_effort": "high",
    "litellm_kwargs": {"thinking": {"type": "disabled"}, "temperature": 0.2},
}


def test_passes_lightrag_config_verbatim_with_max_tokens():
    """lightrag 段用户配置原样透传（thinking/effort 不注入不清空）+ 只注入 max_tokens。"""
    with patch("niu_api.llm_proxy.get_llm_config", return_value=dict(LIGHTRAG_CFG)) as mock_get, \
         patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        cfg = _build_compress_llm_config()
    # 内部 refetch lightrag 段
    mock_get.assert_called_once_with(use_lightrag_config=True)
    assert cfg["model"] == "lightrag-model"
    assert cfg["reasoning_effort"] == "high"  # effort 原样透传（无置空/无降级）
    assert cfg["litellm_kwargs"]["thinking"] == {"type": "disabled"}  # thinking 原样透传（无注入 enabled/disabled）
    assert cfg["litellm_kwargs"]["max_tokens"] == 32000  # 只注入 max_tokens


def test_does_not_mutate_lightrag_config():
    """不修改 get_llm_config 返回的 lightrag 段配置（调用方后续可复用）。"""
    from copy import deepcopy

    src = dict(LIGHTRAG_CFG)
    src["litellm_kwargs"] = dict(LIGHTRAG_CFG["litellm_kwargs"])
    snapshot = deepcopy(src)
    with patch("niu_api.llm_proxy.get_llm_config", return_value=src), \
         patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        _build_compress_llm_config()
    assert src == snapshot
    assert "max_tokens" not in src["litellm_kwargs"]


def test_preserves_other_litellm_kwargs():
    """保留 lightrag 段 litellm_kwargs 其余键（如 temperature/top_p）+ max_tokens。"""
    with patch("niu_api.llm_proxy.get_llm_config", return_value={
        "model": "x",
        "litellm_kwargs": {"temperature": 0.2, "top_p": 0.9},
    }), patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        cfg = _build_compress_llm_config()
    assert cfg["litellm_kwargs"]["temperature"] == 0.2
    assert cfg["litellm_kwargs"]["top_p"] == 0.9
    assert cfg["litellm_kwargs"]["max_tokens"] == 32000
