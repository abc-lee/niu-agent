"""context-manager 绕过 @niu-agent/@end 守则注入和拦截层的单元测试。

背景：同步异步子 Agent 调用改造给所有子 Agent 强制注入守则 + 强制拦截无 @ 前缀的输出，
误伤了 context-manager 的原生 keep=/update=/cursor= 输出格式。本测试验证：
1. context-manager 系统提示词不含守则
2. context-manager 输出 keep=/update=/cursor= 不被拦截（返回 NO_INTERCEPTION）
3. 其他子 Agent（如 file-processor）仍该被注入守则、该被拦截
4. context-manager 同步调用（_is_sync_subagent=True, memory_context=None）不被拦截
"""
from unittest import mock


def test_context_manager_system_prompt_has_no_at_niu_guide():
    """context-manager 的系统提示词里不包含 @niu-agent/@end 守则段"""
    from agent.subagent import build_subagent_system_segments
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER

    static_system, _ = build_subagent_system_segments("context-manager")
    assert _SUBAGENT_ASK_GUIDE_MARKER not in static_system
    assert "@niu-agent" not in static_system
    assert "## 子 Agent 与主 Agent 对话规则" not in static_system


def test_file_processor_system_prompt_still_has_at_niu_guide():
    """file-processor 的系统提示词里仍包含守则段（验证绕过只针对 context-manager）"""
    from agent.subagent import build_subagent_system_segments
    from agent.subagent import _SUBAGENT_ASK_GUIDE_MARKER

    static_system, _ = build_subagent_system_segments("file-processor")
    assert _SUBAGENT_ASK_GUIDE_MARKER in static_system
    assert "@niu-agent" in static_system


def test_context_manager_keep_output_not_intercepted():
    """context-manager 输出 keep=/update=/cursor= 时，拦截层返回 NO_INTERCEPTION"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True  # 同步路径
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
    """context-manager 输出无 @ 前缀时，messages 不被追加 [对话格式错误] 提示"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "context-manager"
    fake_handler._is_sync_subagent = True
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
