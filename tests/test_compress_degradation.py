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


def test_degradation_step1_success():
    """降级第一步（关思考链）成功 → 返回 (方案, 原始 msg_ids, None)。
    函数只执行降级调用，不执行原始调用（原始调用由调用方在调用本函数之前完成）。"""
    from niu_api.compat import _compact_with_degradation_sync

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        # call 1 = 降级第一步，直接返回成功
        return "keep=1,2\nupdate=1|[摘要] summary"

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=[{"role": "user", "content": "[idx:1] msg"}],
        compress_msg_ids=["id1"],
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is not None
    assert "keep=" in result_str
    assert actual_ids == ["id1"]  # 未砍半，返回原始 msg_ids
    assert halved_ids is None
    assert call_count[0] == 1  # 只有降级第一步1次


def test_degradation_step2_success():
    """降级第二步（砍半）成功 → 返回 (方案, 后半段 msg_ids, 前半段 msg_ids)。"""
    from niu_api.compat import _compact_with_degradation_sync

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "COMPACT_TRUNCATED:truncated"  # 降级第一步截断
        return "keep=1\nupdate=1|[摘要] summary"  # 降级第二步成功

    history = [{"role": "user", "content": f"[idx:{i+1}] msg{i+1}"} for i in range(4)]
    msg_ids = ["id1", "id2", "id3", "id4"]

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=history,
        compress_msg_ids=msg_ids,
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is not None
    assert "keep=" in result_str
    # 砍半后 actual_ids 是后半段（target_cut=2, history[2] 是 user → cut_idx=2）
    assert actual_ids == ["id3", "id4"]
    assert halved_ids == ["id1", "id2"]  # 前半段
    assert call_count[0] == 2  # 降级第一步1 + 降级第二步1


def test_degradation_all_fail():
    """全部失败 → 返回 (None, 原始 msg_ids, None)。"""
    from niu_api.compat import _compact_with_degradation_sync

    def mock_call_fn(**kwargs):
        return "COMPACT_TRUNCATED:always truncated"

    history = [{"role": "user", "content": "[idx:1] msg"},
               {"role": "assistant", "content": "[idx:2] reply"},
               {"role": "user", "content": "[idx:3] msg3"},
               {"role": "assistant", "content": "[idx:4] reply4"}]
    msg_ids = ["id1", "id2", "id3", "id4"]

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=history,
        compress_msg_ids=msg_ids,
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is None
    assert actual_ids == msg_ids  # 返回原始
    assert halved_ids is None


def test_degradation_dream_idx_in_halved_range():
    """Force 路径 dream_idx 落在裁剪范围内 → 不执行砍半，报失败。
    dream_idx 是 1-based, cut_idx 是 0-based。
    dream_idx=1 <= cut_idx=2 → dream 边界在 0-based idx 0（前半段），报失败。"""
    from niu_api.compat import _compact_with_degradation_sync

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        # call 1 = 降级第一步，返回截断
        return "COMPACT_TRUNCATED:truncated"

    history = [{"role": "user", "content": f"[idx:{i+1}] msg{i+1}"} for i in range(4)]
    msg_ids = ["id1", "id2", "id3", "id4"]

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=history,
        compress_msg_ids=msg_ids,
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "force_history": [],
                               "last_compress_id": None,
                               "dream_idx_in_force": 1},  # 1-based, <= cut_idx=2
        call_fn=mock_call_fn,
    )
    # dream_idx=1 <= cut_idx=2 → 报失败，不执行砍半
    assert result_str is None
    assert call_count[0] == 1  # 只有降级第一步1次，没有降级第二步


def test_degradation_subagent_error():
    """降级第一步返回 SUBAGENT_ERROR → 报失败。"""
    from niu_api.compat import _compact_with_degradation_sync

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        return "SUBAGENT_ERROR:AuthenticationError: invalid key"

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=[{"role": "user", "content": "[idx:1] msg"}],
        compress_msg_ids=["id1"],
        llm_config={"reasoning_effort": "high", "litellm_kwargs": {"max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is None
    assert call_count[0] == 1  # 只有降级第一步，SUBAGENT_ERROR 直接报失败