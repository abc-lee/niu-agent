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


class TestSplitPromptByTokens:
    """Test prompt splitting for sub-agent overflow protection."""

    def test_short_prompt_no_split(self):
        from agent.subagent import split_prompt_by_tokens
        chunks = split_prompt_by_tokens("Hello world", max_tokens_per_chunk=50000)
        assert len(chunks) == 1
        assert chunks[0] == "Hello world"

    def test_long_prompt_splits(self):
        from agent.subagent import split_prompt_by_tokens
        lines = [f"消息 {i}: 这是一段测试内容用于验证分片功能" for i in range(200)]
        prompt = "\n".join(lines)
        chunks = split_prompt_by_tokens(prompt, max_tokens_per_chunk=200)
        assert len(chunks) >= 2

    def test_empty_prompt_returns_empty_list(self):
        from agent.subagent import split_prompt_by_tokens
        chunks = split_prompt_by_tokens("", max_tokens_per_chunk=50000)
        assert chunks == []

    def test_single_long_line_not_split(self):
        from agent.subagent import split_prompt_by_tokens
        long_line = "测试" * 10000
        chunks = split_prompt_by_tokens(long_line, max_tokens_per_chunk=100)
        assert len(chunks) == 1
        assert chunks[0] == long_line

    def test_chunks_preserve_content(self):
        from agent.subagent import split_prompt_by_tokens
        lines = [f"消息 {i}: 内容" for i in range(50)]
        prompt = "\n".join(lines)
        chunks = split_prompt_by_tokens(prompt, max_tokens_per_chunk=200)
        rejoined = "\n".join(chunks)
        assert rejoined == prompt
