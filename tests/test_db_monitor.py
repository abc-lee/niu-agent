"""db 监测程序路由逻辑单元测试。"""
import os
import sqlite3
import tempfile
from unittest.mock import patch, MagicMock

import niu_api.db_monitor as db_monitor_mod


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
    """@主Agent 消息推入 MainAgentRequestQueue（阶段二改造后）。

    旧机制走 enqueue_supplement；新机制 @niu-agent 拦截和完成通知改走内存队列，
    target==主Agent 的 db 消息（兼容残留）也推入 MainAgentRequestQueue 由链路 A 消费。
    """
    from niu_api.db_monitor import route_message
    from agent.main_agent_request_queue import get_main_agent_request_queue

    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass

    with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
        route_message("主Agent", "file-processor-a1b2", "测试问题")
        mock_enqueue.assert_not_called()

    item = q.pop()
    assert item is not None
    assert "file-processor-a1b2" in item
    assert "测试问题" in item


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
    """目标子 Agent 不在注册表时按 sender 分流（阶段二改造后）。

    - sender==主Agent：孤儿回答丢弃，不推回主 Agent 避免死循环
    - 其他 sender：推回主 Agent supplement queue
    """
    from niu_api.db_monitor import route_message

    # sender==主Agent：丢弃
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = None
        with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
            route_message("unknown-subagent", "主Agent", "测试")
            mock_enqueue.assert_not_called()

    # 其他 sender：推回主 Agent
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = None
        with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
            route_message("unknown-subagent", "other-agent", "测试")
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


# ---- _init_routed_baseline / _poll_messages：用真实临时 sqlite3 db 验证 ----


def _make_temp_db():
    """创建临时 messages.db 并返回路径。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_results TEXT,
            tool_call_id TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()
    return path


def _insert_msg(path, role, content):
    """插入一条消息，返回 rowid。"""
    conn = sqlite3.connect(path)
    cur = conn.execute(
        "INSERT INTO messages (id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (f"msg-{role}-{sqlite3.connect(path).execute('SELECT COUNT(*) FROM messages').fetchone()[0]}",
         role, content, "2026-07-03T00:00:00"),
    )
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def test_init_baseline_records_max_rowid():
    """_init_routed_baseline 应记当前 max(rowid) 作为游标。"""
    path = _make_temp_db()
    try:
        _insert_msg(path, "user", "hi")
        _insert_msg(path, "subagent_msg", "@主Agent [sub1] 第一条")
        _insert_msg(path, "subagent_msg", "@主Agent [sub1] 第二条")
        _insert_msg(path, "assistant", "reply")
        with patch.object(db_monitor_mod, "_db_path", path):
            import asyncio
            asyncio.run(db_monitor_mod._init_routed_baseline())
            # last_seen_rowid 应为最后一条 subagent_msg 的 rowid
            conn = sqlite3.connect(path)
            expected = conn.execute(
                "SELECT MAX(rowid) FROM messages WHERE role='subagent_msg'"
            ).fetchone()[0]
            conn.close()
            assert db_monitor_mod._last_seen_rowid == expected
    finally:
        os.unlink(path)


def test_init_baseline_no_subagent_msgs():
    """db 无 subagent_msg 时 baseline=0。"""
    path = _make_temp_db()
    try:
        _insert_msg(path, "user", "hi")
        _insert_msg(path, "assistant", "reply")
        with patch.object(db_monitor_mod, "_db_path", path):
            import asyncio
            asyncio.run(db_monitor_mod._init_routed_baseline())
            assert db_monitor_mod._last_seen_rowid == 0
    finally:
        os.unlink(path)


def test_poll_messages_routes_new_only():
    """_poll_messages 只路由 rowid > last_seen 的新 subagent_msg。

    阶段二改造后：@主Agent 消息推入 MainAgentRequestQueue（不走 enqueue_supplement）。
    """
    path = _make_temp_db()
    try:
        # 基线：插入 2 条 subagent_msg，记 max rowid
        _insert_msg(path, "subagent_msg", "@主Agent [sub1] 旧1")
        _insert_msg(path, "subagent_msg", "@主Agent [sub1] 旧2")
        with patch.object(db_monitor_mod, "_db_path", path):
            import asyncio
            asyncio.run(db_monitor_mod._init_routed_baseline())
            baseline = db_monitor_mod._last_seen_rowid

            # 清空 MainAgentRequestQueue
            from agent.main_agent_request_queue import get_main_agent_request_queue
            q = get_main_agent_request_queue()
            while q.pop() is not None:
                pass

            # 再插入 2 条新 subagent_msg + 1 条 user（应被 role 过滤）
            _insert_msg(path, "user", "noise")
            _insert_msg(path, "subagent_msg", "@主Agent [sub1] 新1")
            _insert_msg(path, "subagent_msg", "@主Agent [sub1] 新2")

            with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
                asyncio.run(db_monitor_mod._poll_messages())
                # 主 Agent 目标走 MainAgentRequestQueue，不走 enqueue_supplement
                mock_enqueue.assert_not_called()

            # MainAgentRequestQueue 应有 2 条
            assert q.pop() is not None
            assert q.pop() is not None
            assert q.pop() is None

            # last_seen_rowid 应推进到最后一条 subagent_msg 的 rowid
            conn = sqlite3.connect(path)
            expected = conn.execute(
                "SELECT MAX(rowid) FROM messages WHERE role='subagent_msg'"
            ).fetchone()[0]
            conn.close()
            assert db_monitor_mod._last_seen_rowid == expected
            assert expected > baseline
    finally:
        os.unlink(path)


def test_poll_messages_no_new_does_nothing():
    """无新消息时 _poll_messages 不路由、游标不变。"""
    path = _make_temp_db()
    try:
        _insert_msg(path, "subagent_msg", "@主Agent [sub1] 旧")
        with patch.object(db_monitor_mod, "_db_path", path):
            import asyncio
            asyncio.run(db_monitor_mod._init_routed_baseline())
            baseline = db_monitor_mod._last_seen_rowid
            with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
                asyncio.run(db_monitor_mod._poll_messages())
                mock_enqueue.assert_not_called()
            assert db_monitor_mod._last_seen_rowid == baseline
    finally:
        os.unlink(path)


def test_poll_messages_routes_to_subagent_queue():
    """_poll_messages 对 @子Agent 消息推入子 Agent supplement_queue。"""
    path = _make_temp_db()
    try:
        with patch.object(db_monitor_mod, "_db_path", path):
            import asyncio
            asyncio.run(db_monitor_mod._init_routed_baseline())
            _insert_msg(path, "subagent_msg", "@file-processor-a1b2 测试任务")

            mock_queue = MagicMock()
            with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
                mock_registry.get.return_value = MagicMock(supplement_queue=mock_queue)
                asyncio.run(db_monitor_mod._poll_messages())
                mock_queue.push.assert_called_once()
                args, kwargs = mock_queue.push.call_args
                assert "测试任务" in args[0]
                assert kwargs.get("is_terminate") is False
    finally:
        os.unlink(path)


def test_poll_messages_unparsable_advances_cursor():
    """无法解析的 @消息仍推进游标（避免反复尝试）。"""
    path = _make_temp_db()
    try:
        with patch.object(db_monitor_mod, "_db_path", path):
            import asyncio
            asyncio.run(db_monitor_mod._init_routed_baseline())
            _insert_msg(path, "subagent_msg", "no_at_prefix_here")
            with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
                asyncio.run(db_monitor_mod._poll_messages())
                mock_enqueue.assert_not_called()
            # 游标应推进（即使没路由）
            assert db_monitor_mod._last_seen_rowid > 0
    finally:
        os.unlink(path)
