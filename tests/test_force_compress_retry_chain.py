"""截断重试链路回归测试：B1 重试触发/成功，耗尽转 COMPACT_TRUNCATED 信号。"""
import inspect
from unittest.mock import MagicMock

from agent import subagent
from agent.generic.agent_loop import (  # StepOutcome/exhaust 均定义在 agent_loop.py
    StepOutcome,
    agent_runner_loop,
    exhaust,
)
from niu_api.compat import _build_force_prompt, _compact_with_degradation_sync  # noqa: E402


def _mock_resp(finish_reason: str, content: str, tool_calls=None):
    r = MagicMock()
    r.finish_reason = finish_reason
    r.content = content
    r.tool_calls = tool_calls
    r.stream_error = False
    # 关键：MagicMock 的 context_overflow 自动真值会命中 agent_loop.py:1024-1042
    # CONTEXT_OVERFLOW 分支（返回 dict 无 finish_reason 键 → KeyError）——必须显式 False
    # （既有 tests/test_agent_loop_return_messages.py 4b 注释明文记载此陷阱）
    r.context_overflow = False
    r.usage = None
    r.thinking = ""
    return r


def test_agent_loop_b1_retry_recovers_after_truncation():
    """B1：首次 length → 重试；第二次正常 → 成功返回（不报截断）。

    驱动范式参照 tests/test_agent_loop_return_messages.py（handler.dispatch
    返回 StepOutcome(next_prompt=None, should_exit=True) 使无工具轮立即退出）。
    """
    # 注意：不能 yield 后 raise StopIteration(r)（生成器内手动 raise 是 PEP 479 RuntimeError）
    # ——与 Task 3 的 _MockGen 同款：普通类 __next__ 里 raise StopIteration(value) 合法
    class _GenWithValue:
        def __init__(self, content, resp):
            self._content = content
            self._resp = resp
            self._done = False

        def __iter__(self):
            return self

        def __next__(self):
            if not self._done:
                self._done = True
                return self._content
            raise StopIteration(self._resp)

    responses = iter([
        _mock_resp("length", "截断的半截输出"),
        _mock_resp("stop", "keep=1,2,3\nupdate=1|[摘要] xxx"),
    ])

    def _fake_client_chat(messages=None, tools=None):
        r = next(responses)
        return _GenWithValue(r.content, r)

    fake_client = MagicMock()
    fake_client.chat.side_effect = _fake_client_chat

    handler = MagicMock()
    handler._is_subagent = True
    # 关键规避：MagicMock 的 _is_sync_subagent 自动真值会走入子 Agent 拦截 → FORMAT_ERROR
    # （既有 tests/test_agent_loop_return_messages.py 4b 注释明示裸 Mock 会短路）
    handler._is_sync_subagent = False
    handler.max_turns = 5
    handler.dispatch = lambda tool_name, args, response, index=0: StepOutcome(
        data=None, next_prompt=None, should_exit=True
    )

    gen = agent_runner_loop(
        client=fake_client,
        system_prompt="你是 context-manager",
        user_input="压缩",
        handler=handler,
        verbose=False,  # B1 生效路径（所有调用方均 verbose=False）
    )
    result = exhaust(gen)
    # 重试后成功：finish_reason 非 length
    assert result["finish_reason"] == "stop"
    assert result["result"] == "CURRENT_TASK_DONE"
    # 重试消息确实被 append（首次截断原文 + "请大幅缩短"提示在 messages 中）
    # 注意：最终压缩方案只经 StreamEvent("reply", content) 流出、从不 append 进 messages
    # （agent_loop.py 纯文本退出路径）——不能用 keep= 断言 messages
    contents = [m.get("content", "") for m in result["messages"]]
    assert any("截断的半截输出" in c for c in contents)
    assert any("请大幅缩短" in c for c in contents)


def test_call_subagent_maps_length_to_compact_truncated():
    """call_subagent：finish_reason=length → 返回 COMPACT_TRUNCATED: 前缀信号（源码级弱断言）。"""
    src = inspect.getsource(subagent.call_subagent)
    assert "COMPACT_TRUNCATED" in src
    assert "finish_reason" in src


def test_degradation_skips_step1_when_thinking_disabled():
    """thinking 已 disabled（Task 2 注入）→ 降级链跳过 step1 空转，直接 step2 砍半。"""
    llm_config = {"litellm_kwargs": {"max_tokens": 32000, "thinking": {"type": "disabled"}}}

    calls = []

    def _fake_call_fn(**kwargs):
        calls.append(kwargs)
        return "keep=1,2\nupdate=2|[摘要] xxx\ncursor=2"  # step2 成功

    result, msg_ids, halved = _compact_with_degradation_sync(
        agent_name="context-manager",
        prompt="prompt",
        compress_history=[
            {"role": "user", "content": "[idx:1] x"}, {"role": "user", "content": "[idx:2] y"},
            {"role": "user", "content": "[idx:3] z"}, {"role": "user", "content": "[idx:4] w"},
        ],  # 4 条 user → 砍半后 2 条，通过 len<=1 中止闸门（2 条会 abort）
        compress_msg_ids=["a", "b", "c", "d"],
        llm_config=llm_config,
        prompt_builder=_build_force_prompt,
        prompt_builder_kwargs={
            "display_tokens": 1000, "compress_target_tokens": 500, "usage_percent": 80.0,
            "force_history": [{"role": "user", "content": "[idx:1] x"}],
            "last_compress_id": None, "dream_idx_in_force": 0,
        },
        stop_aware=False,
        call_fn=_fake_call_fn,
    )
    # thinking disabled → 跳过 step1（无 step1 调用），直接 step2（1 次调用）
    assert len(calls) == 1
    assert result is not None
    assert "keep=1,2" in result
