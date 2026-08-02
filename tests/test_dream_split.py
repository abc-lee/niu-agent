"""Tests for _split_dream_first_batch — dream-evolver first-batch splitting at user boundaries."""

from niu_api.compat import _split_dream_first_batch


class _Msg:
    """Lightweight message object with .role, .id, .content attributes."""
    def __init__(self, role, mid, content="x"):
        self.role = role
        self.id = mid
        self.content = content


def _msgs(*roles):
    """Build a list of _Msg objects from role strings. IDs are id-0, id-1, etc."""
    return [_Msg(r, f"id-{i}") for i, r in enumerate(roles)]


def _ids(msgs):
    """Extract IDs from a list of _Msg objects."""
    return [m.id for m in msgs]


def _tokens_for(msgs, per_msg_tokens=10):
    """Build a msg_tokens list matching msgs, each with per_msg_tokens."""
    return [per_msg_tokens] * len(msgs)


class TestSplitDreamFirstBatchNoSplit:
    """Tests for scenarios where splitting should NOT occur."""

    def test_below_threshold_no_split(self):
        """Incremental tokens < 50% of context window → no split (None)."""
        msgs = _msgs("user", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=10)  # 40 tokens total
        context_window = 1000  # 40/1000 = 4% << 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is None

    def test_too_few_messages_no_split(self):
        """Fewer than 4 messages → no split even if tokens are high."""
        msgs = _msgs("user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=800)  # 1600 tokens total
        context_window = 1000  # 150% >> 50% but only 2 messages
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is None


class TestSplitDreamFirstBatchSplit:
    """Tests for scenarios where splitting SHOULD occur."""

    def test_split_at_nearest_user(self):
        """8 messages, tokens exceed threshold, split at nearest user to midpoint.

        Messages: [user, assistant, user, assistant, user, assistant, user, assistant]
        mid = 4 (0-indexed), msg[4] = user → split_pos = 4
        first_batch = ids[0:4]
        """
        msgs = _msgs("user", "assistant", "user", "assistant",
                      "user", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=100)  # 800 tokens total
        context_window = 1000  # 80% > 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is not None
        assert result == dream_ids[:4]

    def test_split_picks_closer_user_right(self):
        """When right user is closer to mid, pick right.

        Messages: [user, assistant, assistant, user, assistant, assistant]
        mid = 3, msg[3] = user (right_user). left_user = 0 (user at index 0).
        dist left = 3-0 = 3, dist right = 3-3 = 0 → right closer → split_pos = 3.
        """
        msgs = _msgs("user", "assistant", "assistant", "user", "assistant", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=200)  # 1200 tokens
        context_window = 1000  # 120% > 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is not None
        assert result == dream_ids[:3]

    def test_split_no_user_messages_no_split(self):
        """All tool/assistant messages, no user → no split even if tokens high."""
        msgs = _msgs("assistant", "tool", "assistant", "tool",
                      "assistant", "tool", "assistant", "tool")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=200)  # 1600 tokens
        context_window = 1000  # 150% > 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is None

    def test_split_first_batch_excludes_user_at_split_pos(self):
        """Verify first_batch excludes the user message at the split point.

        Messages: [assistant, assistant, user, assistant, assistant, assistant, user, assistant]
        mid = 4, msg[4] = assistant. right_user = 6 (user). left_user = 2 (user).
        dist left = 4-2 = 2, dist right = 6-4 = 2 → equidistant → pick left → split_pos = 2.
        first_batch = ids[0:2] (excludes the user at index 2).
        """
        msgs = _msgs("assistant", "assistant", "user", "assistant",
                      "assistant", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=100)  # 800 tokens
        context_window = 1000  # 80% > 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is not None
        assert result == dream_ids[:2]
        assert "id-2" not in result

    def test_threshold_just_below_no_split(self):
        """Tokens at 48.8% → no split (below 50% threshold).

        incremental_tokens (488) < context_window * 0.50 (500) → True → no split.
        """
        msgs = _msgs("user", "assistant", "user", "assistant",
                      "user", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=61)  # 488 tokens
        context_window = 1000  # 48.8% < 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is None

    def test_threshold_just_above_splits(self):
        """Tokens at 50.4% → split (at or above 50% threshold).

        incremental_tokens (504) < context_window * 0.50 (500) → False → split.
        """
        msgs = _msgs("user", "assistant", "user", "assistant",
                      "user", "assistant", "user", "assistant")
        dream_ids = _ids(msgs)
        msg_tokens = _tokens_for(msgs, per_msg_tokens=63)  # 504 tokens
        context_window = 1000  # 50.4% >= 50%
        result = _split_dream_first_batch(msgs, dream_ids, msg_tokens, context_window)
        assert result is not None
        assert result == dream_ids[:4]
