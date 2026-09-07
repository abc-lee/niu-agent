# tests/test_compression_summary.py
"""压缩总结：承上启下 prompt、tools=[] 调用、bypass+skip_mirror 落库。"""
from unittest.mock import patch, MagicMock

from agent.generic.agent_loop import (
    _SUMMARY_PROMPT, _run_summary_llm, _persist_summary_without_extract,
)


def test_summary_prompt_is_handoff_oriented():
    """总结 prompt = 承上启下（未完成工作/用户特殊要求/阶段小结），非历史抢救。"""
    assert "当前未完成的工作" in _SUMMARY_PROMPT
    assert "承上启下" in _SUMMARY_PROMPT
    assert "不要调用工具" in _SUMMARY_PROMPT


def test_run_summary_llm_calls_chat_with_no_tools():
    client = MagicMock()
    # exhaust 消费 generator
    def fake_gen():
        class R:
            content = "总结文本"
            stream_error = False
            finish_reason = "stop"
        yield R()
        return R()
    client.chat.return_value = fake_gen()
    with patch("agent.generic.agent_loop.exhaust", side_effect=lambda g: list(g)[-1]):
        text = _run_summary_llm([{"role": "user", "content": "x"}], client)
    assert text == "总结文本"
    # chat 以 tools=[] 调用
    args = client.chat.call_args
    assert args.kwargs.get("tools") == [] or args[1] == []


def test_persist_summary_bypass_extract_and_skip_mirror():
    """总结落库走 ctx._sync_add_message(bypass_at_extract=True, skip_mirror=True)。"""
    ctx = MagicMock()
    ctx._sync_add_message = MagicMock(return_value="msg-1")
    _persist_summary_without_extract(ctx, "总结文本")
    ctx._sync_add_message.assert_called_once_with(
        role="assistant", content="总结文本",
        skip_mirror=True, bypass_at_extract=True,
    )
