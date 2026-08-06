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


def test_halve_history_basic():
    """N/2 截断，向前找 role=user 对齐。"""
    from niu_api.compat import _halve_history

    history = [{"role": "user", "content": "[idx:1] msg1"},
               {"role": "assistant", "content": "[idx:2] msg2"},
               {"role": "user", "content": "[idx:3] msg3"},
               {"role": "assistant", "content": "[idx:4] msg4"}]
    msg_ids = ["id1", "id2", "id3", "id4"]

    halved_h, halved_ids, removed_ids, cut_idx = _halve_history(history, msg_ids)
    # target_cut = 4//2 = 2, compress_history[2] 是 user(idx:3) → cut_idx=2
    assert len(halved_h) == 2
    assert halved_ids == ["id3", "id4"]
    assert removed_ids == ["id1", "id2"]
    assert cut_idx == 2


def test_halve_history_fallback_no_user():
    """找不到 role=user → 从 target_cut 截断。"""
    from niu_api.compat import _halve_history

    history = [{"role": "assistant", "content": "[idx:1] m1"},
               {"role": "assistant", "content": "[idx:2] m2"},
               {"role": "assistant", "content": "[idx:3] m3"},
               {"role": "assistant", "content": "[idx:4] m4"}]
    msg_ids = ["id1", "id2", "id3", "id4"]

    halved_h, halved_ids, removed_ids, cut_idx = _halve_history(history, msg_ids)
    # target_cut=2, 全是 assistant, found_user=False → fallback cut_idx=2
    assert len(halved_h) == 2
    assert cut_idx == 2


def test_halve_history_user_at_index_0():
    """唯一 user 在索引 0 → found_user=True, cut_idx=0, 保留全部。"""
    from niu_api.compat import _halve_history

    history = [{"role": "user", "content": "[idx:1] only user"},
               {"role": "assistant", "content": "[idx:2] reply"},
               {"role": "assistant", "content": "[idx:3] reply2"},
               {"role": "assistant", "content": "[idx:4] reply3"}]
    msg_ids = ["id1", "id2", "id3", "id4"]

    halved_h, halved_ids, removed_ids, cut_idx = _halve_history(history, msg_ids)
    # target_cut=2, 从 2 向前找: idx2=assistant, idx1=assistant, idx0=user → found_user=True, cut_idx=0
    # 不 fallback, 保留全部（cut_idx=0 时 compress_history[0:] = 全部）
    assert len(halved_h) == 4
    assert cut_idx == 0


def test_halve_history_empty():
    """空列表边界。"""
    from niu_api.compat import _halve_history

    halved_h, halved_ids, removed_ids, cut_idx = _halve_history([], [])
    assert halved_h == []
    assert halved_ids == []
    assert removed_ids == []
    assert cut_idx == 0


def test_renumber_history():
    """[idx:N] 重新编号为连续 1, 2, 3...（只替换第一个前缀）"""
    from niu_api.compat import _renumber_history

    history = [{"role": "user", "content": "[idx:51] old msg"},
               {"role": "assistant", "content": "[idx:52] old reply"}]
    result = _renumber_history(history)
    assert result[0]["content"] == "[idx:1] old msg"
    assert result[1]["content"] == "[idx:2] old reply"


def test_renumber_history_no_idx_prefix():
    """没有 [idx:N] 前缀的消息不受影响。"""
    from niu_api.compat import _renumber_history

    history = [{"role": "user", "content": "plain message"}]
    result = _renumber_history(history)
    assert result[0]["content"] == "plain message"