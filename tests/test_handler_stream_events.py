"""测试 NiuHandler 所有 yield 点都返回 StreamEvent 而非裸 str。

TDD: 先写测试，确认失败，再改代码。
"""
import pytest
from unittest.mock import Mock, MagicMock, patch

from agent.generic.agent_loop import StreamEvent, StepOutcome
from agent.handler import NiuHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_events(gen):
    """收集生成器产生的所有 yield 值（StreamEvent 或 str），忽略返回值。"""
    events = []
    try:
        while True:
            events.append(next(gen))
    except StopIteration:
        pass
    return events


def _make_handler(**kwargs):
    """创建一个 NiuHandler 实例，带默认 mock 依赖。"""
    return NiuHandler(
        cwd="/tmp",
        mcp_client=kwargs.get("mcp_client", Mock()),
        disk_engine=kwargs.get("disk_engine", None),
    )


# ---------------------------------------------------------------------------
# _call_subagent_gen 测试
# ---------------------------------------------------------------------------

class TestCallSubagentGen:
    """测试 _call_subagent_gen 方法的 yield 点。"""

    def test_runner_not_initialized_yields_system_event(self):
        """Runner 未初始化时应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        with patch("agent.runner.get_runner", return_value=None):
            gen = handler._call_subagent_gen("test-agent", {"task": "do something"})
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        assert len(stream_events) >= 1, f"应至少有一个 StreamEvent，实际: {events}"
        system_events = [e for e in stream_events if e.type == "system"]
        assert len(system_events) >= 1, f"应有 system 类型 StreamEvent，实际: {stream_events}"
        assert any("Runner not initialized" in e.content for e in system_events), \
            f"system 事件内容应包含 'Runner not initialized'，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_subagent_calling_yields_tool_marker_event(self):
        """子 Agent 调用开始时应 yield StreamEvent('tool_marker', ...)。"""
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        with patch("agent.runner.get_runner", return_value=mock_runner), \
             patch("agent.subagent.call_subagent", return_value="task done"):
            gen = handler._call_subagent_gen("test-agent", {"task": "do something"})
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        tool_marker_events = [e for e in stream_events if e.type == "tool_marker"]
        assert len(tool_marker_events) >= 1, f"应有 tool_marker 类型 StreamEvent，实际: {stream_events}"
        assert any("Calling test-agent" in e.content for e in tool_marker_events), \
            f"tool_marker 事件内容应包含 'Calling test-agent'，实际: {tool_marker_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_subagent_completed_yields_tool_marker_event(self):
        """子 Agent 完成时应 yield StreamEvent('tool_marker', ...)。"""
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        with patch("agent.runner.get_runner", return_value=mock_runner), \
             patch("agent.subagent.call_subagent", return_value="task done"):
            gen = handler._call_subagent_gen("test-agent", {"task": "do something"})
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        tool_marker_events = [e for e in stream_events if e.type == "tool_marker"]
        assert any("completed" in e.content for e in tool_marker_events), \
            f"应有包含 'completed' 的 tool_marker 事件，实际: {tool_marker_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_subagent_error_yields_system_event(self):
        """子 Agent 异常时应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        with patch("agent.runner.get_runner", return_value=mock_runner), \
             patch("agent.subagent.call_subagent", side_effect=RuntimeError("boom")):
            gen = handler._call_subagent_gen("test-agent", {"task": "do something"})
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        system_events = [e for e in stream_events if e.type == "system"]
        assert len(system_events) >= 1, f"应有 system 类型 StreamEvent，实际: {stream_events}"
        assert any("Error" in e.content for e in system_events), \
            f"system 事件内容应包含 'Error'，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_subagent_database_error_yields_system_event(self):
        """event-manager 数据库错误时应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        import tempfile, sqlite3 as real_sqlite3
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create memory.json pointing to tmpdir
            memory_path = Path(tmpdir) / ".niu" / "memory.json"
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text('{"workspace": {"path": "' + tmpdir + '"}}', encoding="utf-8")

            # Create an empty scheduled_tasks.db so that Path(db_path).exists() returns True
            db_path = Path(tmpdir) / "scheduled_tasks.db"
            real_conn = real_sqlite3.connect(str(db_path))
            real_conn.execute(
                "CREATE TABLE IF NOT EXISTS scheduled_tasks ("
                "id INTEGER PRIMARY KEY, content TEXT, status TEXT, "
                "scheduled_at TEXT, created_at TEXT)"
            )
            real_conn.commit()
            real_conn.close()

            # Patch the sqlite3 module's connect directly on the module object
            # so that when handler.py does `import sqlite3`, it gets the patched version
            with patch("agent.runner.get_runner", return_value=mock_runner), \
                 patch("agent.subagent.call_subagent", return_value="reminder set"), \
                 patch.object(Path, "home", return_value=Path(tmpdir)), \
                 patch.object(real_sqlite3, "connect", side_effect=real_sqlite3.Error("db fail")):
                gen = handler._call_subagent_gen("event-manager", {"task": "提醒我明天开会"})
                events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        system_events = [e for e in stream_events if e.type == "system"]
        assert any("Database error" in e.content for e in system_events), \
            f"应有包含 'Database error' 的 system 事件，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_subagent_no_task_found_yields_system_event(self):
        """event-manager 数据库中无任务时应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        import tempfile, sqlite3
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create memory.json pointing to tmpdir
            memory_path = Path(tmpdir) / ".niu" / "memory.json"
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text('{"workspace": {"path": "' + tmpdir + '"}}', encoding="utf-8")

            # Create an empty scheduled_tasks.db (table exists but no rows)
            db_path = Path(tmpdir) / "scheduled_tasks.db"
            conn = sqlite3.connect(str(db_path))
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    id INTEGER PRIMARY KEY,
                    content TEXT,
                    status TEXT,
                    scheduled_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            conn.close()

            with patch("agent.runner.get_runner", return_value=mock_runner), \
                 patch("agent.subagent.call_subagent", return_value="reminder set"), \
                 patch.object(Path, "home", return_value=Path(tmpdir)):
                gen = handler._call_subagent_gen("event-manager", {"task": "提醒我明天开会"})
                events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        system_events = [e for e in stream_events if e.type == "system"]
        assert any("No task found" in e.content for e in system_events), \
            f"应有包含 'No task found' 的 system 事件，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_subagent_verify_task_fail_yields_system_event(self):
        """event-manager 验证任务异常时应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        from pathlib import Path

        # Make Path.home() / ".niu" / "memory.json" exist but raise on read_text
        mock_home_result = Mock(spec=Path)
        mock_niu_dir = Mock(spec=Path)
        mock_memory_json = Mock(spec=Path)
        mock_memory_json.exists.return_value = True
        mock_memory_json.read_text.side_effect = RuntimeError("verify fail")
        mock_niu_dir.__truediv__ = Mock(return_value=mock_memory_json)
        mock_home_result.__truediv__ = Mock(return_value=mock_niu_dir)

        with patch("agent.runner.get_runner", return_value=mock_runner), \
             patch("agent.subagent.call_subagent", return_value="reminder set"), \
             patch.object(Path, "home", return_value=mock_home_result):
            gen = handler._call_subagent_gen("event-manager", {"task": "提醒我明天开会"})
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        system_events = [e for e in stream_events if e.type == "system"]
        assert any("Failed to verify" in e.content for e in system_events), \
            f"应有包含 'Failed to verify' 的 system 事件，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"


# ---------------------------------------------------------------------------
# dispatch 测试
# ---------------------------------------------------------------------------

class TestDispatch:
    """测试 dispatch 方法的 yield 点。"""

    def test_mcp_tool_not_found_yields_system_event(self):
        """MCP 工具未找到时应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        mock_registry = Mock()
        mock_registry.get.return_value = None

        with patch("agent.tool_registry.get_registry", return_value=mock_registry):
            gen = handler.dispatch("some-server/nonexistent_tool", {}, None, index=0)
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        system_events = [e for e in stream_events if e.type == "system"]
        assert len(system_events) >= 1, f"应有 system 类型 StreamEvent，实际: {stream_events}"
        assert any("Tool not found" in e.content for e in system_events), \
            f"system 事件内容应包含 'Tool not found'，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_mcp_tool_executed_yields_tool_marker_event(self):
        """MCP 工具执行成功时应 yield StreamEvent('tool_marker', ...)。"""
        handler = _make_handler()
        mock_registry = Mock()
        mock_func = Mock(return_value={"status": "success", "data": "ok"})
        mock_registry.get.return_value = mock_func

        with patch("agent.tool_registry.get_registry", return_value=mock_registry):
            gen = handler.dispatch("some-server/some_tool", {}, None, index=0)
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        tool_marker_events = [e for e in stream_events if e.type == "tool_marker"]
        assert len(tool_marker_events) >= 1, f"应有 tool_marker 类型 StreamEvent，实际: {stream_events}"
        assert any("executed" in e.content for e in tool_marker_events), \
            f"tool_marker 事件内容应包含 'executed'，实际: {tool_marker_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_mcp_tool_exception_yields_system_event(self):
        """MCP 工具执行异常时应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        mock_registry = Mock()
        mock_func = Mock(side_effect=RuntimeError("tool crash"))
        mock_registry.get.return_value = mock_func

        with patch("agent.tool_registry.get_registry", return_value=mock_registry):
            gen = handler.dispatch("some-server/some_tool", {}, None, index=0)
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        system_events = [e for e in stream_events if e.type == "system"]
        assert len(system_events) >= 1, f"应有 system 类型 StreamEvent，实际: {stream_events}"
        assert any("MCP Error" in e.content for e in system_events), \
            f"system 事件内容应包含 'MCP Error'，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_bare_tool_executed_yields_tool_marker_event(self):
        """裸工具名（无 / 前缀）执行成功时应 yield StreamEvent('tool_marker', ...)。"""
        handler = _make_handler()
        mock_registry = Mock()
        mock_func = Mock(return_value={"status": "success", "data": "ok"})
        mock_registry.get.return_value = mock_func

        with patch("agent.tool_registry.get_registry", return_value=mock_registry):
            gen = handler.dispatch("lightrag-query", {}, None, index=0)
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        tool_marker_events = [e for e in stream_events if e.type == "tool_marker"]
        assert len(tool_marker_events) >= 1, f"应有 tool_marker 类型 StreamEvent，实际: {stream_events}"
        assert any("executed" in e.content for e in tool_marker_events), \
            f"tool_marker 事件内容应包含 'executed'，实际: {tool_marker_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_bare_tool_exception_yields_system_event(self):
        """裸工具名执行异常时应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        mock_registry = Mock()
        mock_func = Mock(side_effect=RuntimeError("bare tool crash"))
        mock_registry.get.return_value = mock_func

        with patch("agent.tool_registry.get_registry", return_value=mock_registry):
            gen = handler.dispatch("lightrag-query", {}, None, index=0)
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        system_events = [e for e in stream_events if e.type == "system"]
        assert len(system_events) >= 1, f"应有 system 类型 StreamEvent，实际: {stream_events}"
        assert any("MCP Error" in e.content for e in system_events), \
            f"system 事件内容应包含 'MCP Error'，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"

    def test_unknown_tool_yields_system_event(self):
        """未知工具应 yield StreamEvent('system', ...)。"""
        handler = _make_handler()
        mock_registry = Mock()
        mock_registry.get.return_value = None

        with patch("agent.tool_registry.get_registry", return_value=mock_registry):
            gen = handler.dispatch("nonexistent_tool_xyz", {}, None, index=0)
            events = _collect_events(gen)

        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        raw_str_events = [e for e in events if isinstance(e, str)]

        system_events = [e for e in stream_events if e.type == "system"]
        assert len(system_events) >= 1, f"应有 system 类型 StreamEvent，实际: {stream_events}"
        assert any("Unknown tool" in e.content for e in system_events), \
            f"system 事件内容应包含 'Unknown tool'，实际: {system_events}"
        assert len(raw_str_events) == 0, f"不应有裸 str yield，实际: {raw_str_events}"
