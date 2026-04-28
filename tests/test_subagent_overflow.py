"""Tests for sub-agent context overflow protection."""
import pytest


class TestCountTokensForText:
    """Test the token counting utility for sub-agent prompts."""

    def test_empty_string_returns_zero(self):
        from agent.subagent import count_tokens_for_text
        assert count_tokens_for_text("") == 0

    def test_short_text_returns_positive(self):
        from agent.subagent import count_tokens_for_text
        tokens = count_tokens_for_text("Hello world")
        assert tokens > 0

    def test_chinese_text_counts_correctly(self):
        from agent.subagent import count_tokens_for_text
        text = "这是一段中文测试文本"
        tokens = count_tokens_for_text(text)
        assert tokens > 0
        assert 3 <= tokens <= 15

    def test_long_text_counts_more(self):
        from agent.subagent import count_tokens_for_text
        short = "Hello world"
        long = "Hello world " * 100
        assert count_tokens_for_text(long) > count_tokens_for_text(short)
