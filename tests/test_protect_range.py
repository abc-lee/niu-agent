"""Tests for _find_protected_range — user-turn-aware protection logic.

After counting N user/assistant messages from the tail, scans upward to find
the nearest role=user message, ensuring the protected range starts at a
complete user-initiated conversation segment.
"""

from niu_api.compat import _find_protected_range


class _Msg:
    """Lightweight message object with .role and .id attributes."""

    def __init__(self, role, mid=""):
        self.role = role
        self.id = mid


def _msgs(*roles):
    """Build a list of _Msg objects from role strings."""
    return [_Msg(r, f"id-{i}") for i, r in enumerate(roles)]


def _dicts(*roles):
    """Build a list of message dicts from role strings."""
    return [{"role": r, "id": f"id-{i}"} for i, r in enumerate(roles)]


class TestFindProtectedRange:
    """Test the _find_protected_range function."""

    def test_no_protection_when_count_zero(self):
        """min_protect_count=0 returns len(messages) — no protection."""
        msgs = _msgs("user", "assistant", "user")
        assert _find_protected_range(msgs, 0) == 3

    def test_basic_user_turn_protection(self):
        """[user, assistant, user, assistant, user, assistant], min=3.

        idx_N=3 (assistant at index 3), scan up finds user at idx 2, protect from 2.
        """
        msgs = _msgs("user", "assistant", "user", "assistant", "user", "assistant")
        assert _find_protected_range(msgs, 3) == 2

    def test_consecutive_user_messages(self):
        """[user, user, assistant, user, assistant], min=2.

        idx_N=3 (user at index 3), scan up: idx 3=user, idx 2=assistant(stop), protect from 3.
        """
        msgs = _msgs("user", "user", "assistant", "user", "assistant")
        assert _find_protected_range(msgs, 2) == 3

    def test_consecutive_user_at_idx_n(self):
        """[assistant, user, user, assistant, user, assistant], min=2.

        idx_N=4 (user at index 4), scan up: idx 4=user, idx 3=assistant(stop), protect from 4.
        Then check idx 2-1 consecutive users are NOT included (separated by assistant at idx 3).
        """
        msgs = _msgs("assistant", "user", "user", "assistant", "user", "assistant")
        assert _find_protected_range(msgs, 2) == 4

    def test_idx_n_is_assistant_finds_user_above(self):
        """[user, assistant, user, assistant, user, assistant], min=3.

        idx_N=3 (assistant), Phase A skips assistant at idx 3, finds user at idx 2, protect from 2.
        This is the critical test for the P0 algorithm bug.
        """
        msgs = _msgs("user", "assistant", "user", "assistant", "user", "assistant")
        assert _find_protected_range(msgs, 3) == 2

    def test_fewer_than_n(self):
        """Only 5 messages, min=10 → return 0 (protect all)."""
        msgs = _msgs("user", "assistant", "user", "assistant", "user")
        assert _find_protected_range(msgs, 10) == 0

    def test_no_user_found_above(self):
        """[assistant, tool, assistant, tool, assistant], min=3.

        idx_N=0 (assistant), scan up finds no user → return idx_N=0.
        """
        msgs = _msgs("assistant", "tool", "assistant", "tool", "assistant")
        assert _find_protected_range(msgs, 3) == 0

    def test_tool_messages_included(self):
        """Tool messages between protected user/assistant are in range.

        [user, assistant, user, tool, assistant], min=2.
        idx_N=2 (user), scan up: idx 2=user(found), idx 1=assistant(stop), protect from 2.
        Tool at idx 3 is within the protected range [2, 5).
        """
        msgs = _msgs("user", "assistant", "user", "tool", "assistant")
        result = _find_protected_range(msgs, 2)
        assert result == 2  # protect from index 2, includes tool at index 3

    def test_single_message(self):
        """Edge case: single message."""
        msgs = _msgs("user")
        assert _find_protected_range(msgs, 1) == 0

    def test_empty_messages(self):
        """Edge case: empty messages list."""
        assert _find_protected_range([], 10) == 0

    def test_dicts_work_too(self):
        """_find_protected_range handles dict messages (no .role attribute)."""
        msgs = _dicts("user", "assistant", "user", "assistant", "user", "assistant")
        assert _find_protected_range(msgs, 3) == 2
