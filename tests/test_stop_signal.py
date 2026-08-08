"""信号灯重设计测试。"""
from unittest.mock import MagicMock, patch


def test_request_stop_all_subagents():
    """request_stop_all_subagents 给所有在跑子 Agent 推 /stop。"""
    from agent.runner import request_stop_all_subagents
    mock_q1 = MagicMock()
    mock_q2 = MagicMock()
    with patch("agent.runner.SubagentRegistry") as mock_registry:
        mock_registry.list_running.return_value = [
            MagicMock(unique_name="a-1111", supplement_queue=mock_q1, source="user"),
            MagicMock(unique_name="b-2222", supplement_queue=mock_q2, source="user"),
        ]
        request_stop_all_subagents()
        mock_q1.push.assert_called_once_with("/stop", is_terminate=True, sender="主Agent")
        mock_q2.push.assert_called_once_with("/stop", is_terminate=True, sender="主Agent")


def test_request_stop_all_subagents_empty():
    """无在跑子 Agent 时不崩溃。"""
    from agent.runner import request_stop_all_subagents
    with patch("agent.runner.SubagentRegistry") as mock_registry:
        mock_registry.list_running.return_value = []
        request_stop_all_subagents()  # 不应抛异常


def test_request_stop_still_works():
    """现有 request_stop 仍有效（只对主 Agent）。"""
    from agent.runner import clear_stop, is_stop_requested, request_stop
    clear_stop()
    assert not is_stop_requested()
    request_stop()
    assert is_stop_requested()
    clear_stop()
    assert not is_stop_requested()


def test_request_stop_all_single_failure_continues():
    """单个子 Agent push 失败不中断，继续推其他。"""
    from agent.runner import request_stop_all_subagents
    mock_q1 = MagicMock()
    mock_q1.push.side_effect = RuntimeError("push 失败")
    mock_q2 = MagicMock()
    with patch("agent.runner.SubagentRegistry") as mock_registry:
        mock_registry.list_running.return_value = [
            MagicMock(unique_name="a-1111", supplement_queue=mock_q1, source="user"),
            MagicMock(unique_name="b-2222", supplement_queue=mock_q2, source="user"),
        ]
        request_stop_all_subagents()  # 不应抛异常
        mock_q1.push.assert_called_once()
        mock_q2.push.assert_called_once_with("/stop", is_terminate=True, sender="主Agent")
