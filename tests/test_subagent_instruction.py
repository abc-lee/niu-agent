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


def test_import_error_logs_warning_and_returns_true(monkeypatch):
    """E4-06：ImportError（niu_api 未启动——环境预期态）→ logger.warning + 仍返回 True。

    与推送真异常分开记日志（warning/error 两级）；return True 语义保持
    （3 调用方不检查返回值，推送失败不影响子 Agent 循环）。
    """
    import sys

    from loguru import logger

    from agent.subagent import _maybe_push_subagent_instruction

    monkeypatch.setitem(sys.modules, "niu_api.internal.subagent_event_bus", None)
    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        result = _maybe_push_subagent_instruction("file-processor-a1b2", "请处理 test.txt")
    finally:
        logger.remove(sink_id)
    assert result is True
    assert any("指令推送" in m and "file-processor-a1b2" in m for m in messages), (
        f"ImportError 应记 warning（环境未启动预期态），实际: {messages}"
    )


def test_push_exception_logs_error_and_returns_true():
    """E4-06：推送真异常（RuntimeError）→ logger.error（含异常文本）+ 仍返回 True。"""
    from loguru import logger

    from agent.subagent import _maybe_push_subagent_instruction

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    try:
        with patch("niu_api.internal.subagent_event_bus.notify_subagent_event_sync",
                   side_effect=RuntimeError("push boom")):
            result = _maybe_push_subagent_instruction("file-processor-a1b2", "请处理 test.txt")
    finally:
        logger.remove(sink_id)
    assert result is True
    assert any("push boom" in m and "指令推送失败" in m for m in messages), (
        f"真异常应记 error 含异常文本，实际: {messages}"
    )
