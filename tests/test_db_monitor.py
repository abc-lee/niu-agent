"""db 监测程序路由逻辑单元测试。"""
from unittest.mock import patch, MagicMock


def test_parse_at_message():
    """解析 @消息格式：@目标 [发送者名] 内容。"""
    from niu_api.db_monitor import parse_at_message
    target, sender, content = parse_at_message("@主Agent [file-processor-a1b2] 这个 PDF 是扫描件吗？")
    assert target == "主Agent"
    assert sender == "file-processor-a1b2"
    assert content == "这个 PDF 是扫描件吗？"


def test_parse_at_message_no_sender():
    """主 Agent 发给子 Agent 的消息可能无 [发送者名]。"""
    from niu_api.db_monitor import parse_at_message
    target, sender, content = parse_at_message("@file-processor-a1b2 试试换个路径")
    assert target == "file-processor-a1b2"
    assert sender == ""
    assert content == "试试换个路径"


def test_parse_at_message_stop():
    """/stop 指令解析。"""
    from niu_api.db_monitor import parse_at_message
    target, sender, content = parse_at_message("@file-processor-a1b2 /stop")
    assert target == "file-processor-a1b2"
    assert content == "/stop"


def test_route_to_main_agent():
    """@主Agent 消息推入主 Agent supplement queue。"""
    from niu_api.db_monitor import route_message
    with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
        route_message("主Agent", "file-processor-a1b2", "测试问题")
        mock_enqueue.assert_called_once()
        call_args = mock_enqueue.call_args[0][0]
        assert "@主Agent" in call_args
        assert "file-processor-a1b2" in call_args
        assert "测试问题" in call_args


def test_route_to_subagent_normal():
    """@子名 普通消息推入子 Agent supplement queue。"""
    from niu_api.db_monitor import route_message
    mock_queue = MagicMock()
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = MagicMock(supplement_queue=mock_queue)
        route_message("file-processor-a1b2", "主Agent", "补充内容")
        mock_queue.push.assert_called_once_with("补充内容", is_terminate=False, sender="主Agent")


def test_route_to_subagent_stop():
    """@子名 /stop 推入子 Agent supplement queue 标记 is_terminate=True。"""
    from niu_api.db_monitor import route_message
    mock_queue = MagicMock()
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = MagicMock(supplement_queue=mock_queue)
        route_message("file-processor-a1b2", "主Agent", "/stop")
        mock_queue.push.assert_called_once_with("/stop", is_terminate=True, sender="主Agent")


def test_route_target_not_found():
    """目标子 Agent 不在注册表，推回主 Agent。"""
    from niu_api.db_monitor import route_message
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = None
        with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
            route_message("unknown-subagent", "主Agent", "测试")
            mock_enqueue.assert_called_once()
            call_args = mock_enqueue.call_args[0][0]
            assert "unknown-subagent" in call_args
            assert "已不存在" in call_args


def test_route_to_subagent_multi_hyphen_type():
    """多连字符类型子 Agent 名（如 context-manager）能正确路由。"""
    from niu_api.db_monitor import route_message
    mock_queue = MagicMock()
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = MagicMock(supplement_queue=mock_queue)
        route_message("context-manager-c3d4", "主Agent", "压缩吧")
        mock_queue.push.assert_called_once_with("压缩吧", is_terminate=False, sender="主Agent")
