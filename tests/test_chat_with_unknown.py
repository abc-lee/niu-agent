"""chat-with-* 调用不存在的子 Agent → 明确错误反馈。"""
import pytest


def _consume_generator(gen):
    """消费 dispatch 生成器，返回 StepOutcome（StopIteration.value）。"""
    ret = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        ret = e.value
    return ret


def test_chat_with_unknown_agent_rejected(monkeypatch):
    """不存在的 agent → StepOutcome error（主 Agent 收到明确反馈）。"""
    from agent.handler import NiuHandler
    h = NiuHandler.__new__(NiuHandler)
    # 模拟 get_subagent_config 返回空 dict（不存在）
    monkeypatch.setattr("agent.subagent.get_subagent_config", staticmethod(lambda name: {}))

    # 直接调用路由逻辑（提取为可测函数或走 dispatch）
    from agent.handler import _check_chat_with_agent_exists
    ok, msg = _check_chat_with_agent_exists("nonexistent-agent")
    assert ok is False
    assert "不存在" in msg


def test_chat_with_existing_agent_allowed(monkeypatch):
    """存在的 agent（~/.niu/agents/nutritionist.md）→ 放行。"""
    from agent.handler import _check_chat_with_agent_exists
    monkeypatch.setattr("agent.subagent.get_subagent_config",
                        staticmethod(lambda name: {"description": "x"} if name == "nutritionist" else {}))
    ok, msg = _check_chat_with_agent_exists("nutritionist")
    assert ok is True


def test_dispatch_chat_with_unknown_agent_error(monkeypatch):
    """dispatch 集成：chat-with-nonexistent-agent → StepOutcome error + next_prompt 含"不存在"。"""
    from agent.handler import NiuHandler
    monkeypatch.setattr("agent.subagent.get_subagent_config", staticmethod(lambda name: {}))

    handler = NiuHandler(mcp_client=None)
    gen = handler.dispatch("chat-with-nonexistent-agent", {}, None, index=0)
    ret = _consume_generator(gen)

    assert ret is not None
    data = ret.data if hasattr(ret, "data") else ret
    assert isinstance(data, dict)
    assert data.get("status") == "error"
    assert "不存在" in data.get("msg", "")
    assert "不存在" in (getattr(ret, "next_prompt", "") or "")


def test_resume_running_async_distinct_message(monkeypatch):
    """resume 路径对 state='running' 的异步实例 → 报"正在运行（异步）"而非误导的"找不到挂起"。"""
    import types

    from agent.subagent import call_subagent
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    monkeypatch.setattr("agent.subagent.get_subagent_config",
                        staticmethod(lambda name: {"description": "x"}))
    monkeypatch.setattr("agent.subagent.build_subagent_system_segments",
                        staticmethod(lambda name: ("static", "")))
    monkeypatch.setattr("agent.runner.create_client",
                        staticmethod(lambda llm_config: types.SimpleNamespace(backend=types.SimpleNamespace())))
    monkeypatch.setattr("agent.subagent._build_subagent_tools_schema",
                        staticmethod(lambda **kw: {}))
    monkeypatch.setattr("agent.subagent._read_context_window_tokens", staticmethod(lambda: 0))

    sq = SubagentSupplementQueue("test-async-run")
    name = SubagentRegistry.register("test-agent", supplement_queue=sq, is_sync=False,
                                     force_unique_name="test-agent-0abc")
    try:
        result = call_subagent(
            agent_name="test-agent",
            task="",
            answer="hello",
            answer_unique_name=name,
            llm_config={},
            mcp_client=None,
        )
        assert "正在运行" in result
        assert "异步" in result
        assert "找不到挂起" not in result
    finally:
        SubagentRegistry.unregister(name)
