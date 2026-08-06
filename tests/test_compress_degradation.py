# tests/test_compress_degradation.py
"""压缩输出超长三级降级策略测试。"""
import copy


def test_build_degraded_config_disables_thinking():
    """关闭 thinking，降 reasoning_effort 一级。"""
    from niu_api.compat import _build_degraded_config

    llm_config = {
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}, "max_tokens": 32000},
    }
    result = _build_degraded_config(llm_config)
    assert result["litellm_kwargs"]["thinking"] == {"type": "disabled"}
    assert result["reasoning_effort"] == "medium"
    # max_tokens 保留
    assert result["litellm_kwargs"]["max_tokens"] == 32000


def test_build_degraded_config_effort_map():
    """reasoning_effort 降级映射：xhigh→high→medium→low→minimal。"""
    from niu_api.compat import _build_degraded_config

    cases = {"xhigh": "high", "high": "medium", "medium": "low", "low": "minimal"}
    for orig, expected in cases.items():
        result = _build_degraded_config({"reasoning_effort": orig, "litellm_kwargs": {}})
        assert result["reasoning_effort"] == expected, f"{orig} should degrade to {expected}"


def test_build_degraded_config_minimal_not_degraded():
    """minimal/none/空 不再降。"""
    from niu_api.compat import _build_degraded_config

    for val in ["minimal", "none", "", None]:
        result = _build_degraded_config({"reasoning_effort": val, "litellm_kwargs": {}})
        assert result["reasoning_effort"] == val


def test_build_degraded_config_deepcopy():
    """不修改原始 llm_config。"""
    from niu_api.compat import _build_degraded_config

    original = {"reasoning_effort": "high", "litellm_kwargs": {"thinking": {"type": "enabled"}}}
    original_snapshot = copy.deepcopy(original)
    _build_degraded_config(original)
    assert original == original_snapshot
