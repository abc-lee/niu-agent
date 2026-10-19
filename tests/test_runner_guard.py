"""P2a 主 Agent 单执行体守卫测试（计划 §5.1，全 mock，不连真实 LLM）。

覆盖点：
1. acquire / release 基本语义
2. 重入被拒 + finally 释放
3. 错误 token 不得清除他人 owner
4. 生成器正常耗尽后释放
5. next() 后 close() 释放
6. 第二个生成器被拒，第一个关闭后 owner 空
7. 实现体抛异常后释放
8. 未迭代的生成器 close() 不 acquire
9. 非绑定 `NiuRunner.chat(self_mock, ...)` 兼容（既有 self_mock 夹具范式）

全局态 `agent.runner._active_main_owner` 由 tests/conftest.py 的 autouse fixture
在每个用例前后置 None。
"""

import contextlib
import inspect
from unittest import mock

import pytest

from agent import runner as runner_mod
from agent.generic.agent_loop import StreamEvent
from agent.runner import (
    NiuRunner,
    acquire_main_agent_thread,
    release_main_agent_thread,
)


def _owner():
    """当前持有主 Agent owner 的令牌（None = 空闲）。"""
    return runner_mod._active_main_owner


def _self_mock():
    """与 tests/test_sync_subagent_exit_guard.py、tests/test_fold_view_refresh.py 同款夹具。"""
    self_mock = mock.MagicMock()
    self_mock._im_channel_id = ""
    self_mock.default_model = "test-model"
    self_mock.base_tools_schema = []
    self_mock._assemble_tools_schema.return_value = []
    self_mock.should_push_im.return_value = False
    return self_mock


def _fake_loop(events, result=None, gate=None):
    """伪造 agent_runner_loop：先吐 events，可选卡在 gate，最后 return 结果 dict。"""

    def gen():
        yield from events
        if gate is not None:
            gate.wait()
        return result if result is not None else {"result": "CURRENT_TASK_DONE", "messages": []}

    return gen()


@contextlib.contextmanager
def _chat_env(agent_runner_loop_impl):
    """驱动真实 `_chat_impl` 所需的最小 mock 集（与既有 self_mock 用例一致）。"""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(runner_mod, "cleanup_suspended_sync_subagents"))
        stack.enter_context(
            mock.patch.object(runner_mod, "agent_runner_loop", side_effect=agent_runner_loop_impl)
        )
        stack.enter_context(mock.patch.object(runner_mod, "is_stop_requested", return_value=False))
        stack.enter_context(mock.patch.object(runner_mod, "clear_stop"))
        stack.enter_context(mock.patch("agent.subagent._read_context_window_tokens", return_value=0))
        yield


# ---------------------------------------------------------------------------
# 1. acquire / release
# ---------------------------------------------------------------------------

def test_acquire_returns_token_and_release_clears_owner():
    assert _owner() is None

    token = acquire_main_agent_thread()
    try:
        assert token is not None
        assert _owner() is token
    finally:
        release_main_agent_thread(token)

    assert _owner() is None


# ---------------------------------------------------------------------------
# 2. 重入抛错（finally 释放）
# ---------------------------------------------------------------------------

def test_reentry_is_rejected_and_finally_releases():
    token = acquire_main_agent_thread()
    try:
        with pytest.raises(RuntimeError) as exc:
            acquire_main_agent_thread()
        assert "main agent already running" in str(exc.value)
        # 被拒的 acquire 不得夺走 / 清空现有 owner
        assert _owner() is token
    finally:
        release_main_agent_thread(token)

    assert _owner() is None


# ---------------------------------------------------------------------------
# 3. 错误 token 不清除
# ---------------------------------------------------------------------------

def test_release_with_foreign_token_does_not_clear_owner():
    token = acquire_main_agent_thread()
    try:
        release_main_agent_thread(object())  # 陌生令牌
        assert _owner() is token
        release_main_agent_thread(None)  # 空令牌
        assert _owner() is token
    finally:
        release_main_agent_thread(token)

    assert _owner() is None


# ---------------------------------------------------------------------------
# 4. 正常耗尽释放
# ---------------------------------------------------------------------------

def test_normal_exhaustion_releases_owner():
    self_mock = _self_mock()

    with _chat_env(lambda **kwargs: _fake_loop([StreamEvent("reply", "hi")])):
        gen = NiuRunner.chat(self_mock, session_id="s1", user_input="hi")
        chunks = list(gen)

        assert chunks == ["hi"]
        assert _owner() is None
        assert inspect.getgeneratorstate(gen) == "GEN_CLOSED"


# ---------------------------------------------------------------------------
# 5. next() 后 close() 释放
# ---------------------------------------------------------------------------

def test_close_after_first_next_releases_owner():
    self_mock = _self_mock()

    with _chat_env(lambda **kwargs: _fake_loop([StreamEvent("reply", "hi"), StreamEvent("reply", "there")])):
        gen = NiuRunner.chat(self_mock, session_id="s1", user_input="hi")

        assert next(gen) == "hi"
        assert _owner() is not None  # 迭代中必须持有 owner

        gen.close()
        assert _owner() is None
        assert inspect.getgeneratorstate(gen) == "GEN_CLOSED"


# ---------------------------------------------------------------------------
# 6. 第二个生成器被拒，g1 关闭后 owner 空
# ---------------------------------------------------------------------------

def test_second_generator_is_rejected_until_first_closes():
    self_mock = _self_mock()

    with _chat_env(lambda **kwargs: _fake_loop([StreamEvent("reply", "hi")])):
        g1 = NiuRunner.chat(self_mock, session_id="s1", user_input="a")
        assert next(g1) == "hi"
        owner = _owner()
        assert owner is not None

        g2 = NiuRunner.chat(self_mock, session_id="s2", user_input="b")
        with pytest.raises(RuntimeError, match="main agent already running"):
            next(g2)

        # 被拒者不得夺走 owner，也不得触发 release
        assert _owner() is owner

        g1.close()
        assert _owner() is None


# ---------------------------------------------------------------------------
# 7. 实现体抛异常释放
# ---------------------------------------------------------------------------

def test_impl_exception_releases_owner():
    self_mock = _self_mock()

    def _boom(**kwargs):
        def gen():
            yield StreamEvent("reply", "partial")
            raise RuntimeError("llm exploded")

        return gen()

    with _chat_env(_boom):
        gen = NiuRunner.chat(self_mock, session_id="s1", user_input="hi")
        with pytest.raises(RuntimeError, match="llm exploded"):
            list(gen)

        assert _owner() is None
        assert inspect.getgeneratorstate(gen) == "GEN_CLOSED"


# ---------------------------------------------------------------------------
# 8. 未迭代 close 不 acquire
# ---------------------------------------------------------------------------

def test_close_without_iteration_never_acquires():
    self_mock = _self_mock()

    gen = NiuRunner.chat(self_mock, session_id="s1", user_input="hi")
    assert _owner() is None  # 生成器体尚未执行

    gen.close()
    assert _owner() is None
    assert inspect.getgeneratorstate(gen) == "GEN_CLOSED"


# ---------------------------------------------------------------------------
# 9. 非绑定 NiuRunner.chat(self_mock, ...) 兼容
# ---------------------------------------------------------------------------

def test_non_bound_chat_with_self_mock_still_works():
    """守卫包装必须保持 `NiuRunner.chat(self_mock, ...)` 可用（既有测试范式）。"""
    captured = {}

    def fake_loop(**kwargs):
        captured.update(kwargs)

        def gen():
            if False:
                yield  # 成为 generator 但不 yield 任何值
            return {"result": "CURRENT_TASK_DONE", "messages": []}

        return gen()

    self_mock = _self_mock()

    with _chat_env(fake_loop):
        chunks = list(NiuRunner.chat(self_mock, session_id="s1", user_input="hi"))

        assert chunks == []
        assert captured.get("on_tool_round_refresh") is self_mock._on_tool_round_refresh
        assert _owner() is None
