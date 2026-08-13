"""截断重试链路回归测试：B1 重试恢复、call_subagent COMPACT_TRUNCATED 映射存在性（弱断言）、thinking-disabled 降级跳过 step1。"""
import inspect
from unittest.mock import MagicMock

from agent import subagent
from agent.generic.agent_loop import (  # exhaust 定义在 agent_loop.py
    agent_runner_loop,
    exhaust,
)
from niu_api.compat import _build_force_prompt, _compact_with_degradation_sync


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

    无工具轮走纯文本退出路径（不经 dispatch）——handler 只需
    _is_subagent/_is_sync_subagent/max_turns 属性。
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
    # max_turns 会被 agent_runner_loop 内部覆盖（默认 40），无需设置

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
        ],  # 4 条 user → 砍半后 2 条，通过 len<=1 中止闸门（2 条→砍半 1 条→len<=1 中止闸门，故需 4 条）
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
    # 区分断言：第三返回值 = step2 成功路径的 removed_msg_ids 前半段（4 条 → cut_idx=2 → ["a","b"]）；
    # 若产品代码回归为 thinking_enabled 恒 True，step1 误执行并成功 → 第三返回值是 None → 断言挂
    assert halved == ["a", "b"]
