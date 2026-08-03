"""Tests for dream-evolver proactive trigger threshold calculation."""

from agent.runner import _calc_dream_trigger_threshold


class TestCalcDreamTriggerThreshold:
    """Tests for _calc_dream_trigger_threshold."""

    def test_default_200k_window(self):
        """200K context window → (100000 - 8000) / 12000 = 7.7 → max(10, 7) = 10
        """
        result = _calc_dream_trigger_threshold(200000)
        assert result == 10

    def test_small_window(self):
        """32K context window → (16000 - 8000) / 12000 = 0.67 → max(10, 0) = 10
        """
        result = _calc_dream_trigger_threshold(32000)
        assert result == 10

    def test_zero_window_returns_default(self):
        """Zero or negative context window → default 10."""
        assert _calc_dream_trigger_threshold(0) == 10
        assert _calc_dream_trigger_threshold(-1) == 10

    def test_large_window_no_upper_clamp(self):
        """2M context window → (1000000 - 8000) / 12000 = 82.7 → 82 (no upper clamp).
        """
        result = _calc_dream_trigger_threshold(2000000)
        assert result == 82

    def test_medium_window(self):
        """500K context window → (250000 - 8000) / 12000 = 20.2 → 20.
        """
        result = _calc_dream_trigger_threshold(500000)
        assert result == 20
