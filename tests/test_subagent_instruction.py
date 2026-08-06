"""子 Agent 指令/回答推送纯函数守卫测试。

不调真实 LLM，只验证 _maybe_push_subagent_instruction 的条件分支 + 副作用。
"""
from unittest.mock import MagicMock, patch


def test_pushes_when_handler_has_unique_name_and_content():
    """handler 有 unique_name + 有内容 → 推送，返回 True。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction(handler, "请处理 test.txt")
    assert result is True
    mock_notify.assert_called_once_with("file-processor-a1b2", "instruction", {"content": "请处理 test.txt"})


def test_pushes_when_unique_name_string_and_content():
    """直接传 unique_name 字符串 + 有内容 → 推送（推送点 3 续答路径用）。"""
    from agent.subagent import _maybe_push_subagent_instruction
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction("file-processor-a1b2", "这是主 Agent 的回答")
    assert result is True
    mock_notify.assert_called_once_with("file-processor-a1b2", "instruction", {"content": "这是主 Agent 的回答"})


def test_skips_when_content_empty():
    """内容为空 → 不推送（续答路径 answer="" 不推）。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction(handler, "")
    assert result is False
    mock_notify.assert_not_called()


def test_skips_when_content_none():
    """内容为 None → 不推送（续答路径 initial_user_content=None）。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = "file-processor-a1b2"
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction(handler, None)
    assert result is False
    mock_notify.assert_not_called()


def test_skips_when_no_unique_name():
    """handler 无 unique_name → 不推送。"""
    from agent.subagent import _maybe_push_subagent_instruction
    handler = MagicMock()
    handler._subagent_unique_name = None
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction(handler, "请处理文件")
    assert result is False
    mock_notify.assert_not_called()


def test_skips_when_unique_name_empty_string():
    """unique_name 为空字符串 → 不推送。"""
    from agent.subagent import _maybe_push_subagent_instruction
    with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync") as mock_notify:
        result = _maybe_push_subagent_instruction("", "请处理文件")
    assert result is False
    mock_notify.assert_not_called()
