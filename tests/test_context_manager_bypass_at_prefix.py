"""context-manager @前缀拦截层绕过开关的单元测试。

背景：拦截层曾按 agent 名字（unique_name == "context-manager"）无条件绕过，误伤模式一
（多轮工具交互）：2026-07-22 模式一压缩中 LLM 空响应被误判为压缩完成，提前退出且游标误推进。
整改后绕过由调用方显式传 call_subagent(bypass_at_prefix=True) 开启：
- 模式二/三（一轮出 keep=/update=/cursor= 方案）：传 True 绕过，行为与整改前一致
- 模式一（多轮工具）：默认 False，走标准 @end/FORMAT_ERROR 结束判断
本测试验证：
1. context-manager 系统提示词不含 @niu-agent 守则（保持不变）
2. bypass_at_prefix=True 时输出 keep= 方案不被拦截（模式二/三行为锁定）
3. bypass_at_prefix=False 时空响应走 FORMAT_ERROR 追问（模式一新行为）
4. 其他子 Agent（file-processor）仍被注入守则、仍被拦截（不受影响）
5. call_subagent 把 bypass_at_prefix 参数透传到 handler._bypass_at_prefix
"""
from unittest import mock


def test_context_manager_system_prompt_has_no_at_niu_guide():
    """context-manager 的系统提示词里不包含 @niu-agent/@end 守则段"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER, build_subagent_system_segments

    static_system, _ = build_subagent_system_segments("context-manager")
    assert _SUBAGENT_ASK_GUIDE_MARKER not in static_system
    assert "@niu-agent" not in static_system
    assert "## 子 Agent 与主 Agent 对话规则" not in static_system


def test_file_processor_system_prompt_still_has_at_niu_guide():
    """file-processor 的系统提示词里仍包含守则段（验证绕过只针对 context-manager）"""
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER, build_subagent_system_segments

    static_system, _ = build_subagent_system_segments("file-processor")
    assert _SUBAGENT_ASK_GUIDE_MARKER in static_system
    assert "@niu-agent" in static_system


def test_context_manager_keep_output_not_intercepted():
    """模式二/三（bypass_at_prefix=True）：输出 keep=/update=/cursor= 时，拦截层返回 NO_INTERCEPTION"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True  # 同步路径
    fake_handler._bypass_at_prefix = True  # 一轮出方案显式绕过（模式二/三路径）
    messages = [
        {"role": "system", "content": "你是 context-manager"},
        {"role": "user", "content": "压缩这些消息"},
    ]
    content = "<analysis>分析...</analysis>\nkeep=1,2,3\nupdate=4|[摘要] xxx\ncursor=3"

    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,  # 同步调用，memory_context=None
    )
    assert result == (agent_loop.NO_INTERCEPTION, None)
    # 验证 messages 没有被追加格式错误提示
    assert len(messages) == 2  # 原始两条不动


def test_context_manager_bypass_doesnt_append_format_error():
    """模式二/三（bypass_at_prefix=True）：输出无 @ 前缀时，messages 不被追加 [对话格式错误] 提示"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True
    fake_handler._bypass_at_prefix = True  # 一轮出方案显式绕过（模式二/三路径）
    messages = [
        {"role": "system", "content": "你是 context-manager"},
        {"role": "user", "content": "压缩"},
    ]
    original_len = len(messages)
    content = "keep=1,5,10\nupdate=2|[摘要] xxx\ncursor=10"

    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )
    assert result == (agent_loop.NO_INTERCEPTION, None)
    assert len(messages) == original_len  # 关键：messages 不变，没有追加 FORMAT_ERROR 提示


def test_file_processor_still_intercepted_when_no_at_prefix():
    """file-processor 输出无 @ 前缀无 tool_calls 时，仍返回 FORMAT_ERROR（验证绕过只针对 context-manager）"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "file-processor-a1b2"
    fake_handler._is_sync_subagent = False
    messages = [
        {"role": "system", "content": "你是 file-processor"},
        {"role": "user", "content": "处理文件"},
    ]
    content = "我处理完了"  # 无 @ 前缀

    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),  # 异步路径
    )
    assert result == (agent_loop.FORMAT_ERROR, None)


def test_context_manager_mode1_no_bypass_goes_format_error():
    """模式一（_bypass_at_prefix=False）：空响应走标准 FORMAT_ERROR 追问，不再按名字绕过。

    回归 2026-07-22 事故：模式一压缩第 7 轮 LLM 把 delete_messages 泄漏进 thinking
    （正式响应 content="" + tool_calls=[]），按名字绕过使程序误判压缩完成、游标误推进。
    """
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True
    fake_handler._bypass_at_prefix = False  # 模式一：默认 False，走标准结束判断
    messages = [
        {"role": "system", "content": "你是 context-manager"},
        {"role": "user", "content": "压缩这些消息"},
    ]
    content = ""  # 空响应（事故触发场景：工具调用泄漏进 thinking 后的正式响应）

    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )
    assert result == (agent_loop.FORMAT_ERROR, None)
    # 验证 messages 被追加 assistant 空响应 + FORMAT_ERROR user 追问
    assert len(messages) == 4
    assert messages[-2] == {"role": "assistant", "content": ""}
    assert messages[-1]["role"] == "user"
    assert "对话格式错误" in messages[-1]["content"]


def test_call_subagent_passes_bypass_at_prefix_to_handler(monkeypatch):
    """call_subagent(bypass_at_prefix=True) 时，内部 handler._bypass_at_prefix 为 True"""
    from agent import subagent

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema,
                 max_turns=20, initial_user_content=None, context_window_tokens=0,
                 context_fifo_threshold=0, history=None, **kwargs):
        captured["handler"] = handler
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

    import agent.runner as runner_mod
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

    subagent.call_subagent(
        agent_name="test-agent",
        task="t",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
        bypass_at_prefix=True,
    )
    assert captured["handler"]._bypass_at_prefix is True


def test_call_subagent_default_bypass_at_prefix_false(monkeypatch):
    """不传 bypass_at_prefix 时，handler._bypass_at_prefix 为 False（走标准 @end 拦截）"""
    from agent import subagent

    captured = {}

    def mock_run(client, system_prompt, user_input, handler, tools_schema,
                 max_turns=20, initial_user_content=None, context_window_tokens=0,
                 context_fifo_threshold=0, history=None, **kwargs):
        captured["handler"] = handler
        return ("done", {"result": "CURRENT_TASK_DONE", "data": "ok"})

    monkeypatch.setattr(subagent, "_run_agent_loop", mock_run)
    monkeypatch.setattr(subagent, "get_subagent_prompt", lambda name: "system")
    monkeypatch.setattr(subagent, "get_subagent_config", lambda name: {})
    monkeypatch.setattr(subagent, "get_subagent_mcp_tools_schema", lambda name: [])

    import agent.runner as runner_mod
    monkeypatch.setattr(runner_mod, "create_client", lambda cfg: None)
    monkeypatch.setattr(runner_mod, "get_tools_schema", lambda include_main_only=False: [])
    monkeypatch.setattr(subagent, "_read_context_window_tokens", lambda: 200000)

    subagent.call_subagent(
        agent_name="test-agent",
        task="t",
        llm_config={"apikey": "test", "apibase": "http://test", "model": "test"},
    )
    assert captured["handler"]._bypass_at_prefix is False
