"""Tests for dream-evolver proactive trigger threshold calculation.

Tests the dynamic threshold algorithm `_calc_dream_trigger_threshold_dynamic`,
which estimates per-turn token cost from post-compress messages and derives
the trigger threshold from the context window's 30% incremental budget.
"""

from types import SimpleNamespace

from agent.runner import _calc_dream_trigger_threshold_dynamic


def _make_msgs(n_turns, tokens_per_msg=3000):
    """构造 n_turns 轮对话（每轮 1 条 user + 1 条 assistant = 2 条消息）。

    返回 (msgs, tokens) 二元组：
    - msgs: SimpleNamespace 列表，每条有 role 和 content
    - tokens: 与 msgs 等长的 token 估算列表，每条 tokens_per_msg
    """
    msgs = []
    for i in range(n_turns):
        msgs.append(SimpleNamespace(role='user', content=f'user message {i}'))
        msgs.append(SimpleNamespace(role='assistant', content=f'assistant reply {i}'))
    tokens = [tokens_per_msg] * len(msgs)
    return msgs, tokens


class TestCalcDreamTriggerThresholdDynamic:
    """Tests for _calc_dream_trigger_threshold_dynamic."""

    def test_dynamic_threshold_200k(self):
        """200K 窗口 + 19 轮（38 条消息）+ 每条 3000 tokens。

        avg = (38 * 3000) / 19 = 6000
        threshold = int(60000 / 6000) = 10  → 下限兜底 10
        """
        msgs, tokens = _make_msgs(19, tokens_per_msg=3000)
        result = _calc_dream_trigger_threshold_dynamic(200000, msgs, tokens)
        assert result == 10

    def test_dynamic_threshold_200k_lower_avg(self):
        """200K 窗口 + 19 轮 + 每条 1500 tokens。

        avg = (38 * 1500) / 19 = 3000
        threshold = int(60000 / 3000) = 20
        """
        msgs, tokens = _make_msgs(19, tokens_per_msg=1500)
        result = _calc_dream_trigger_threshold_dynamic(200000, msgs, tokens)
        assert result == 20

    def test_dynamic_threshold_small_window(self):
        """32K 窗口 + 19 轮 + 每条 1500 tokens。

        avg = 3000
        threshold = int(9600 / 3000) = 3  → max(10, 3) = 10
        """
        msgs, tokens = _make_msgs(19, tokens_per_msg=1500)
        result = _calc_dream_trigger_threshold_dynamic(32000, msgs, tokens)
        assert result == 10

    def test_dynamic_threshold_min_samples(self):
        """2 轮（4 条消息）→ turn_count < 3 → 直接返回保底 10。"""
        msgs, tokens = _make_msgs(2, tokens_per_msg=3000)
        result = _calc_dream_trigger_threshold_dynamic(200000, msgs, tokens)
        assert result == 10

    def test_dynamic_threshold_floor_ceiling(self):
        """avg 极高→下限 10；avg 极低→上限 50。

        极高：每条 100000 tokens，19 轮
            avg = (38 * 100000) / 19 = 200000
            threshold = int(60000 / 200000) = 0  → max(10, 0) = 10
        极低：每条 1 token，19 轮（avg 兜底到 1000）
            avg = max(1000, (38 * 1) / 19) = max(1000, 2) = 1000
            threshold = int(60000 / 1000) = 60  → min(50, 60) = 50
        """
        # 极高 avg → 下限 10
        msgs_high, tokens_high = _make_msgs(19, tokens_per_msg=100000)
        result_high = _calc_dream_trigger_threshold_dynamic(200000, msgs_high, tokens_high)
        assert result_high == 10

        # 极低 avg → 上限 50
        msgs_low, tokens_low = _make_msgs(19, tokens_per_msg=1)
        result_low = _calc_dream_trigger_threshold_dynamic(200000, msgs_low, tokens_low)
        assert result_low == 50

    def test_dynamic_threshold_tool_heavy(self):
        """200K 窗口 + 10 轮 + 每条 8000 tokens（工具密集型）。

        avg = (20 * 8000) / 10 = 16000
        threshold = int(60000 / 16000) = 3  → max(10, 3) = 10
        """
        msgs, tokens = _make_msgs(10, tokens_per_msg=8000)
        result = _calc_dream_trigger_threshold_dynamic(200000, msgs, tokens)
        assert result == 10
