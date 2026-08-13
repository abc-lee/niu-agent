"""压缩专用 LLM 配置测试：首次调用即注入 max_tokens + 关闭思考链。"""
from unittest.mock import patch

from niu_api.compat import _build_compress_llm_config  # noqa: E402


def test_injects_max_tokens_and_disables_thinking():
    """注入 max_tokens（_read_max_output_tokens 值）+ thinking disabled。"""
    base = {"model": "x", "litellm_kwargs": {"thinking": {"type": "enabled"}}}
    with patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        cfg = _build_compress_llm_config(base)
    assert cfg["litellm_kwargs"]["max_tokens"] == 32000
    assert cfg["litellm_kwargs"]["thinking"] == {"type": "disabled"}


def test_does_not_mutate_original_config():
    """不修改原始 llm_config（调用方复用同一 llm_config 于其他路径）。"""
    base = {"model": "x", "litellm_kwargs": {"thinking": {"type": "enabled"}}}
    with patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        _build_compress_llm_config(base)
    assert base["litellm_kwargs"] == {"thinking": {"type": "enabled"}}
    assert "max_tokens" not in base["litellm_kwargs"]


def test_preserves_other_litellm_kwargs():
    """保留原 litellm_kwargs 其余键（如 temperature/top_p）。"""
    base = {"model": "x", "litellm_kwargs": {"temperature": 0.2, "top_p": 0.9}}
    with patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        cfg = _build_compress_llm_config(base)
    assert cfg["litellm_kwargs"]["temperature"] == 0.2
    assert cfg["litellm_kwargs"]["top_p"] == 0.9
