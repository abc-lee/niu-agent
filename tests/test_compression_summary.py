# tests/test_compression_summary.py
"""压缩总结：承上启下 prompt、tools=[] 调用、bypass+skip_mirror 落库。"""
from unittest.mock import patch, MagicMock

from agent.generic.agent_loop import (
    _SUMMARY_PROMPT, _run_summary_llm, _persist_summary_without_extract,
    _triplet_messages, _persist_compression_triplet, _COMPRESSION_DONE_HINT,
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


def test_triplet_messages_nonempty_summary_three_rows():
    """三件套构造（非空总结）：序 = [user 准备提示][assistant 总结][user 完成提示]。"""
    msgs = _triplet_messages("总结文本")
    assert len(msgs) == 3
    assert msgs[0] == {"role": "user", "content": _SUMMARY_PROMPT}
    assert msgs[1] == {"role": "assistant", "content": "总结文本"}
    assert msgs[2] == {"role": "user", "content": _COMPRESSION_DONE_HINT}


def test_triplet_messages_empty_summary_two_rows_no_empty_assistant():
    """空总结只两条（无 assistant 行）——防空 content="" 进 client.chat 致 provider 400。"""
    msgs = _triplet_messages("")
    assert len(msgs) == 2
    assert [m["role"] for m in msgs] == ["user", "user"]
    assert msgs[0]["content"] == _SUMMARY_PROMPT
    assert msgs[1]["content"] == _COMPRESSION_DONE_HINT


def test_persist_compression_triplet_three_rows_order_and_flags():
    """三件套落库：3 次调用序 = [准备 user][总结 assistant][完成 user]，全 skip_mirror+bypass。"""
    ctx = MagicMock()
    ctx._sync_add_message = MagicMock(return_value="msg-1")
    _persist_compression_triplet(ctx, "总结文本")
    assert ctx._sync_add_message.call_count == 3
    calls = [c.kwargs for c in ctx._sync_add_message.call_args_list]
    assert [(c["role"], c["content"]) for c in calls] == [
        ("user", _SUMMARY_PROMPT),
        ("assistant", "总结文本"),
        ("user", _COMPRESSION_DONE_HINT),
    ]
    for c in calls:
        assert c["skip_mirror"] is True
        assert c["bypass_at_extract"] is True


def test_persist_compression_triplet_empty_summary_two_rows():
    """空总结 → 只落两条（跳过 assistant 行）。"""
    ctx = MagicMock()
    ctx._sync_add_message = MagicMock(return_value="msg-1")
    _persist_compression_triplet(ctx, "")
    assert ctx._sync_add_message.call_count == 2
    calls = [c.kwargs for c in ctx._sync_add_message.call_args_list]
    assert [(c["role"], c["content"]) for c in calls] == [
        ("user", _SUMMARY_PROMPT),
        ("user", _COMPRESSION_DONE_HINT),
    ]


def test_persist_compression_triplet_row_failure_does_not_block_later_rows():
    """单行写失败（返回 None）→ 不阻断后续行（三行非事务，warning 累积）。"""
    ctx = MagicMock()
    ctx._sync_add_message = MagicMock(side_effect=[None, "msg-2", "msg-3"])
    _persist_compression_triplet(ctx, "总结文本")
    assert ctx._sync_add_message.call_count == 3  # 首行失败仍落完后续两行


def test_triplet_prefix_parity():
    """slicer 本地三件套前缀与 agent_loop 两条常量 startswith 绑定（双副本漂移必红）。

    slicer 保持零依赖不导入 agent_loop，判据为本地常量前缀副本——本契约
    保证任一副本改动即红（spec 2026-09-07 §4 / B P2-2）。
    """
    from agent.context_assembler.slicer import _COMPRESSION_TRIPLET_PREFIXES

    assert len(_COMPRESSION_TRIPLET_PREFIXES) == 2
    assert _SUMMARY_PROMPT.startswith(_COMPRESSION_TRIPLET_PREFIXES[0])
    assert _COMPRESSION_DONE_HINT.startswith(_COMPRESSION_TRIPLET_PREFIXES[1])
