"""on_before_llm 回调单元测试。

验证：
1. on_before_llm 在 LLM 调用前被调用
2. 每轮 LLM 调用前都会调用（不是只首轮）
3. 不传 on_before_llm 时正常工作（向后兼容）
4. on_before_llm 抛异常时仅 warning、对话继续（异常容错）

关键 mock 要点（三轮 C1/C2/C3 修复）：
- C1：patch 目标必须是源模块 agent.runner（is_stop_requested/clear_stop/drain_supplement
  在 agent/runner.py L45/L50/L121 定义），agent_loop.py 是函数内 import（L511），
  模块命名空间不存在这些属性，patch agent.generic.agent_loop 会 AttributeError
- C2：client.chat 返回的 generator 必须 yield + return 同一个 response
  （agent_loop L317-322 exhaust 取 return value；只 yield 不 return 会拿到 None）
- C3：dispatch side_effect 必须返回 generator 实例（_make_dispatch_gen() 调用），
  不能返回 generator 函数本身（否则 next() 会 TypeError）
- tool_calls 用 Mock 而非 MagicMock（MagicMock(name=...) 的 name 是构造参数不是属性）
- 所有测试统一 patch _intercept_at_prefix_content（M2 修复，verbose=False 路径必调）
"""
from contextlib import ExitStack
from unittest.mock import MagicMock, Mock, patch

from agent.generic.agent_loop import StepOutcome, agent_runner_loop, exhaust


def _common_patches(stack: ExitStack):
    """统一的 patch 集合，所有测试都用（C1 修复：patch 源模块 agent.runner）"""
    # is_stop_requested/clear_stop/drain_supplement 在 agent.runner 定义，
    # agent_loop.py L511 函数内 import——patch 源模块才生效
    stack.enter_context(patch("agent.runner.is_stop_requested", return_value=False))
    stack.enter_context(patch("agent.runner.clear_stop"))
    stack.enter_context(patch("agent.runner.drain_supplement"))
    stack.enter_context(patch("agent.generic.agent_loop._enforce_message_budget", side_effect=lambda m: m))
    stack.enter_context(patch("agent.generic.agent_loop._fifo_prune", return_value=0))
    stack.enter_context(patch("agent.generic.agent_loop._placeholderize_tool_outputs", return_value=0))
    stack.enter_context(patch("agent.generic.agent_loop.count_messages_tokens", return_value=100))
    # M2 修复：verbose=False 路径必调 _intercept_at_prefix_content，统一 patch 返回无拦截
    stack.enter_context(patch("agent.generic.agent_loop._intercept_at_prefix_content", return_value=(False, None)))


def _make_response(content="test response", tool_calls=None):
    """构造一个 mock LLM response"""
    response = MagicMock()
    response.content = content
    response.tool_calls = tool_calls  # None 表示无 tool_calls
    response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    response.context_overflow = False
    return response


def _make_chat_gen(response):
    """构造一个 client.chat 返回的 generator（C2 修复：yield + return 同一个 response）

    agent_loop L663 `response = yield from response_gen`（verbose=True）
    或 L666 `response = exhaust(response_gen)`（verbose=False）取 return value。
    只 yield 不 return 时 exhaust 拿 StopIteration.value=None，后续 response.content 会 AttributeError。
    """
    def _gen(*args, **kwargs):
        yield response
        return response  # 关键：exhaust 取 return value
    return _gen()


def _make_tool_call(tc_id: str, tool_name: str, args_json: str):
    """构造一个 Mock tool_call（用 Mock 而非 MagicMock，避免 name 参数歧义）"""
    tc = Mock()
    tc.id = tc_id
    tc.function = Mock()
    tc.function.name = tool_name
    tc.function.arguments = args_json
    return tc


def _make_dispatch_gen(outcome: StepOutcome):
    """构造一个 dispatch generator 工厂（C3 修复：调用 _gen() 返回 generator 实例）

    agent_loop L851 `gen = handler.dispatch(...)` + L857 `exhaust(gen)` 取 return value。
    dispatch 必须是 generator 实例，不能是 generator 函数（否则 next() 会 TypeError）。
    参考 tests/test_dynamic_injection_per_turn.py:_make_handler.mock_dispatch 写法。
    """
    def _gen(*args, **kwargs):
        yield  # 让 dispatch 成为 generator
        return outcome
    return _gen()  # 关键：调用 _gen() 返回 generator 实例，不是返回函数本身


def test_on_before_llm_called_before_first_llm_call():
    """首轮 LLM 调用前，on_before_llm 被调用一次"""
    client = MagicMock()
    client.chat.return_value = _make_chat_gen(_make_response(tool_calls=None))

    handler = MagicMock()
    handler.max_turns = 5
    handler._last_prompt_tokens = 0
    handler._done_hooks = []

    call_log = []

    def on_before_llm(messages, turn):
        call_log.append(("before_llm", turn, len(messages)))

    with ExitStack() as stack:
        _common_patches(stack)
        gen = agent_runner_loop(
            client=client,
            system_prompt="test system",
            user_input="hello",
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
            on_before_llm=on_before_llm,
        )
        exhaust(gen)  # 用 exhaust 取 return value，不用 list(gen)

    assert len(call_log) >= 1, "on_before_llm 应被调用至少一次"
    assert call_log[0] == ("before_llm", 1, 2), f"首次调用应是 turn=1, messages含system+user=2条，实际: {call_log[0]}"


def test_on_before_llm_called_every_turn():
    """多轮 LLM 调用前，on_before_llm 每轮都被调用"""
    client = MagicMock()
    # 第一轮：返回 tool_calls，让循环继续
    response1 = _make_response(
        content="调用工具",
        tool_calls=[_make_tool_call("tc1", "test_tool", '{"x": 1}')],
    )
    # 第二轮：无 tool_calls，退出
    response2 = _make_response(content="done", tool_calls=None)

    responses = [response1, response2]
    def _chat_gen(*args, **kwargs):
        resp = responses.pop(0)
        yield resp
        return resp  # C2 修复：yield + return 同一个 response
    client.chat.side_effect = [_chat_gen(), _chat_gen()]

    handler = MagicMock()
    handler.max_turns = 5
    handler._last_prompt_tokens = 0
    handler._done_hooks = []
    # C2+C3 修复：dispatch side_effect 每次调用都返回新 generator 实例
    handler.dispatch = MagicMock(side_effect=lambda *a, **kw: _make_dispatch_gen(
        StepOutcome(data={"content": "tool result", "tool_use_id": "tc1"}, next_prompt="继续", should_exit=False)
    ))

    call_log = []

    def on_before_llm(messages, turn):
        call_log.append(turn)

    with ExitStack() as stack:
        _common_patches(stack)
        gen = agent_runner_loop(
            client=client,
            system_prompt="test system",
            user_input="hello",
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
            on_before_llm=on_before_llm,
            on_turn_end=lambda m, t, n: t,  # no-op：原样返回 tools_schema（契约 (messages, tools_schema, turn) -> tools_schema）
        )
        exhaust(gen)

    assert len(call_log) == 2, f"应被调用 2 次（每轮 LLM 调用前），实际: {len(call_log)}"
    assert call_log == [1, 2], f"应按 turn 顺序调用，实际: {call_log}"


def test_on_before_llm_none_backward_compatible():
    """不传 on_before_llm 时，agent_runner_loop 正常工作（向后兼容）

    用 exhaust(gen) 取 return value 验证最终 result。
    response.tool_calls=None 走 CURRENT_TASK_DONE 分支（非 EXITED）。
    """
    client = MagicMock()
    client.chat.return_value = _make_chat_gen(_make_response(tool_calls=None))

    handler = MagicMock()
    handler.max_turns = 5
    handler._last_prompt_tokens = 0
    handler._done_hooks = []

    with ExitStack() as stack:
        _common_patches(stack)
        gen = agent_runner_loop(
            client=client,
            system_prompt="test system",
            user_input="hello",
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
            # 不传 on_before_llm
        )
        final = exhaust(gen)  # 取 return value

    # response.tool_calls=None → agent_loop L977 或 L1053 return {"result": "CURRENT_TASK_DONE", ...}
    # 不是 EXITED（EXITED 在 L917 should_exit=True 路径）
    assert isinstance(final, dict), f"final 应是 dict（generator return value），实际: {type(final)}"
    assert final.get("result") == "CURRENT_TASK_DONE", \
        f"无 tool_calls 应走 CURRENT_TASK_DONE 分支，实际 result: {final.get('result')}"


def test_on_before_llm_exception_does_not_break_loop():
    """on_before_llm 抛异常时 agent_loop 继续（注入失败仅 warning，对话继续）—— M3 修复"""
    client = MagicMock()
    client.chat.return_value = _make_chat_gen(_make_response(tool_calls=None))

    handler = MagicMock()
    handler.max_turns = 5
    handler._last_prompt_tokens = 0
    handler._done_hooks = []

    def on_before_llm_raises(messages, turn):
        raise RuntimeError("injection failed")

    with ExitStack() as stack:
        _common_patches(stack)
        gen = agent_runner_loop(
            client=client,
            system_prompt="test system",
            user_input="hello",
            handler=handler,
            tools_schema=[],
            max_turns=5,
            verbose=False,
            on_before_llm=on_before_llm_raises,
        )
        final = exhaust(gen)

    # on_before_llm 抛异常被 agent_loop try/except 捕获（logger.exception），对话继续
    # client.chat 仍被调用，最终正常返回
    assert client.chat.called, "on_before_llm 抛异常后 client.chat 仍应被调用"
    assert isinstance(final, dict), f"final 应是 dict，实际: {type(final)}"
    assert final.get("result") == "CURRENT_TASK_DONE", \
        f"无 tool_calls 应走 CURRENT_TASK_DONE 分支，实际 result: {final.get('result')}"
