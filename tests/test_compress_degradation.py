# tests/test_compress_degradation.py
"""压缩输出超长三级降级策略测试。"""
import copy


def test_build_degraded_config_passes_lightrag_verbatim():
    """降级第一步配置：lightrag 段用户配置原样透传 + 只注入 max_tokens（thinking/effort 不注入不降级）。"""
    from unittest.mock import patch

    from niu_api.compat import _build_degraded_config

    lightrag_cfg = {
        "model": "lightrag-model",
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "disabled"}, "temperature": 0.2},
    }
    with patch("niu_api.llm_proxy.get_llm_config", return_value=dict(lightrag_cfg)), \
         patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        result = _build_degraded_config()
    assert result["reasoning_effort"] == "high"  # effort 原样透传（effort_map 已删，无降级）
    assert result["litellm_kwargs"]["thinking"] == {"type": "disabled"}  # thinking 原样透传（无注入）
    assert result["litellm_kwargs"]["max_tokens"] == 32000  # 只注入 max_tokens
    assert result["litellm_kwargs"]["temperature"] == 0.2


def test_build_degraded_config_no_effort_filter():
    """lightrag 段 effort 任意值原样透传（程序不预设过滤——minimal/none/high/max 均不干预）。"""
    from unittest.mock import patch

    from niu_api.compat import _build_degraded_config

    for val in ["minimal", "none", "high", "max", "", None]:
        with patch("niu_api.llm_proxy.get_llm_config",
                   return_value={"reasoning_effort": val, "litellm_kwargs": {}}), \
             patch("niu_api.compat._read_max_output_tokens", return_value=32000):
            result = _build_degraded_config()
        assert result["reasoning_effort"] == val


def test_build_degraded_config_deepcopy():
    """不修改 get_llm_config 返回的 lightrag 段配置。"""
    from unittest.mock import patch

    from niu_api.compat import _build_degraded_config

    original = {"reasoning_effort": "high", "litellm_kwargs": {"thinking": {"type": "enabled"}}}
    original_snapshot = copy.deepcopy(original)
    with patch("niu_api.llm_proxy.get_llm_config", return_value=original), \
         patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        _build_degraded_config()
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
    """门控 True（thinking enabled）→ 降级第一步成功 → 返回 (方案, 原始 msg_ids, None)。
    函数只执行降级调用，不执行原始调用（原始调用由调用方在调用本函数之前完成）。
    含 SUBAGENT_ERROR 分支覆盖（step1 LLM 调用失败 → 报失败不降级）。"""
    from unittest.mock import patch

    from niu_api.compat import _compact_with_degradation_sync

    llm_config = {
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}, "max_tokens": 32000},
    }
    lightrag_cfg = {
        "model": "m", "apikey": "k", "apibase": "http://x", "type": "openai",
        "provider": "", "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}},
    }
    builder_mocks = (
        patch("niu_api.llm_proxy.get_llm_config", return_value=dict(lightrag_cfg)),
        patch("niu_api.compat._read_max_output_tokens", return_value=32000),
    )
    kwargs = {
        "agent_name": "context-manager",
        "prompt": "original prompt",
        "compress_history": [{"role": "user", "content": "[idx:1] msg"}],
        "compress_msg_ids": ["id1"],
        "llm_config": llm_config,
        "prompt_builder": lambda **kw: "rebuilt prompt",
        "prompt_builder_kwargs": {"display_tokens": 1000, "compress_target_tokens": 500,
                                  "usage_percent": 80, "compress_history": []},
    }

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        # call 1 = 降级第一步，直接返回成功
        return "keep=1,2\nupdate=1|[摘要] summary"

    with builder_mocks[0], builder_mocks[1]:
        result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
            call_fn=mock_call_fn, **kwargs
        )
    assert result_str is not None
    assert "keep=" in result_str
    assert actual_ids == ["id1"]  # 未砍半，返回原始 msg_ids
    assert halved_ids is None
    assert call_count[0] == 1  # 只有降级第一步1次

    # SUBAGENT_ERROR 分支覆盖：step1 返回 SUBAGENT_ERROR → 报失败不降级
    err_calls = [0]
    def mock_err_fn(**kwargs):
        err_calls[0] += 1
        return "SUBAGENT_ERROR:AuthenticationError: invalid key"

    with builder_mocks[0], builder_mocks[1]:
        err_str, err_ids, err_halved = _compact_with_degradation_sync(
            call_fn=mock_err_fn, **kwargs
        )
    assert err_str is None
    assert err_ids == ["id1"]
    assert err_halved is None
    assert err_calls[0] == 1  # SUBAGENT_ERROR 直接报失败，只 step1 1 次


def test_degradation_step2_success():
    """门控 True（thinking enabled）→ 降级第二步（砍半）成功 → 返回 (方案, 后半段 msg_ids, 前半段 msg_ids)。
    含 SUBAGENT_ERROR 分支覆盖（step2 LLM 调用失败 → 报失败不降级）。"""
    from unittest.mock import patch

    from niu_api.compat import _compact_with_degradation_sync

    llm_config = {
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}, "max_tokens": 32000},
    }
    lightrag_cfg = {
        "model": "m", "apikey": "k", "apibase": "http://x", "type": "openai",
        "provider": "", "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}},
    }
    builder_mocks = (
        patch("niu_api.llm_proxy.get_llm_config", return_value=dict(lightrag_cfg)),
        patch("niu_api.compat._read_max_output_tokens", return_value=32000),
    )
    history = [{"role": "user", "content": f"[idx:{i+1}] msg{i+1}"} for i in range(4)]
    msg_ids = ["id1", "id2", "id3", "id4"]
    kwargs = {
        "agent_name": "context-manager",
        "prompt": "original prompt",
        "compress_history": history,
        "compress_msg_ids": msg_ids,
        "llm_config": llm_config,
        "prompt_builder": lambda **kw: "rebuilt prompt",
        "prompt_builder_kwargs": {"display_tokens": 1000, "compress_target_tokens": 500,
                                  "usage_percent": 80, "compress_history": []},
    }

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return "COMPACT_TRUNCATED:truncated"  # 降级第一步截断
        return "keep=1\nupdate=1|[摘要] summary"  # 降级第二步成功

    with builder_mocks[0], builder_mocks[1]:
        result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
            call_fn=mock_call_fn, **kwargs
        )
    assert result_str is not None
    assert "keep=" in result_str
    # 砍半后 actual_ids 是后半段（target_cut=2, history[2] 是 user → cut_idx=2）
    assert actual_ids == ["id3", "id4"]
    assert halved_ids == ["id1", "id2"]  # 前半段
    assert call_count[0] == 2  # 降级第一步1 + 降级第二步1

    # SUBAGENT_ERROR 分支覆盖：step1 截断 → step2 返回 SUBAGENT_ERROR → 报失败不降级
    err_calls = [0]
    def mock_err_fn(**kwargs):
        err_calls[0] += 1
        return "SUBAGENT_ERROR:AuthenticationError: invalid key" if err_calls[0] == 2 \
            else "COMPACT_TRUNCATED:truncated"

    with builder_mocks[0], builder_mocks[1]:
        err_str, err_ids, err_halved = _compact_with_degradation_sync(
            call_fn=mock_err_fn, **kwargs
        )
    assert err_str is None
    assert err_ids == msg_ids
    assert err_halved is None
    assert err_calls[0] == 2  # step1 截断 + step2 SUBAGENT_ERROR


def test_degradation_all_fail():
    """门控 False（thinking disabled）→ step1 不执行，直接 step2 砍半 → 仍截断 → 全部失败返回 None。

    断言门控 False 语义：lightrag 段默认 disabled → 降级链实际手段 = halve history。
    （不断言调用计数——R6B：计数由 step1/step2_success 用例覆盖，此处语义即可。）
    """
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
        llm_config={"reasoning_effort": "high",
                    "litellm_kwargs": {"thinking": {"type": "disabled"}, "max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    assert result_str is None
    assert actual_ids == msg_ids  # 返回原始
    assert halved_ids is None


def test_degradation_dream_idx_in_halved_range():
    """门控 True（thinking enabled）→ Force 路径 dream_idx 落在裁剪范围内 → 不执行砍半，报失败。
    dream_idx 是 1-based, cut_idx 是 0-based。
    dream_idx=1 <= cut_idx=2 → dream 边界在 0-based idx 0（前半段），报失败。"""
    from unittest.mock import patch

    from niu_api.compat import _compact_with_degradation_sync

    llm_config = {
        "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}, "max_tokens": 32000},
    }
    lightrag_cfg = {
        "model": "m", "apikey": "k", "apibase": "http://x", "type": "openai",
        "provider": "", "reasoning_effort": "high",
        "litellm_kwargs": {"thinking": {"type": "enabled"}},
    }

    call_count = [0]
    def mock_call_fn(**kwargs):
        call_count[0] += 1
        # call 1 = 降级第一步，返回截断
        return "COMPACT_TRUNCATED:truncated"

    history = [{"role": "user", "content": f"[idx:{i+1}] msg{i+1}"} for i in range(4)]
    msg_ids = ["id1", "id2", "id3", "id4"]

    with patch("niu_api.llm_proxy.get_llm_config", return_value=dict(lightrag_cfg)), \
         patch("niu_api.compat._read_max_output_tokens", return_value=32000):
        result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
            agent_name="context-manager",
            prompt="original prompt",
            compress_history=history,
            compress_msg_ids=msg_ids,
            llm_config=llm_config,
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


def test_degradation_halve_too_small_aborts():
    """门控 False（thinking disabled）→ 跳过 step1 → step2 砍半后历史 ≤1 → 中止报失败。

    原 subagent_error 用例改写（R7B P3 + R8B P3-3）：SUBAGENT_ERROR 分支覆盖移入
    step1_success/step2_success；本用例改测 'halve too small abort'（1 条 history
    halve 后 ≤1 中止，call_count 0 非 1）——去掉计数断言（计数由 step1/step2_success 覆盖）。"""
    from niu_api.compat import _compact_with_degradation_sync

    def mock_call_fn(**kwargs):
        raise AssertionError("门控 False 时 step1 不应执行，step2 砍半 ≤1 也不应调用 LLM")

    result_str, actual_ids, halved_ids = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="original prompt",
        compress_history=[{"role": "user", "content": "[idx:1] msg"}],
        compress_msg_ids=["id1"],
        llm_config={"reasoning_effort": "high",
                    "litellm_kwargs": {"thinking": {"type": "disabled"}, "max_tokens": 32000}},
        prompt_builder=lambda **kw: "rebuilt prompt",
        prompt_builder_kwargs={"display_tokens": 1000, "compress_target_tokens": 500,
                               "usage_percent": 80, "compress_history": []},
        call_fn=mock_call_fn,
    )
    # 1 条 history → halve 后仍 1 条 → len<=1 中止 → 报失败，LLM 零调用
    assert result_str is None
    assert actual_ids == ["id1"]
    assert halved_ids is None
