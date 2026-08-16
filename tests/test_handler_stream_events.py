"""测试 NiuHandler 所有 yield 点都返回 StreamEvent 而非裸 str。

TDD: 先写测试，确认失败，再改代码。
"""
from unittest.mock import Mock, patch

from agent.generic.agent_loop import StreamEvent
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


def _collect_events_with_return(gen):
    """收集生成器产生的所有 yield 值，并返回生成器返回值（StopIteration.value）。"""
    events = []
    return_value = None
    try:
        while True:
            events.append(next(gen))
    except StopIteration as e:
        return_value = e.value
    return events, return_value


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

        import sqlite3 as real_sqlite3
        import tempfile
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

        import sqlite3
        import tempfile
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

    def test_subagent_verify_fail_injected_into_display_result(self):
        """E4-11：event-manager 验证失败（DB 无任务）→ 失败文本注入 chat-with 结果流。

        主 Agent 下一轮可见验证失败（display_result——不走 next_prompt——
        防 test_working_memory_removal 白名单回归）；system yield 保留（verbose 调试通道）。
        """
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        import sqlite3
        import tempfile
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
                events, return_value = _collect_events_with_return(gen)

        # chat-with 结果流（StepOutcome.data["result"]）含验证失败标记 + 失败原因 + 原始内容
        assert return_value is not None, "应返回 StepOutcome"
        result_text = return_value.data["result"]
        assert "[event-manager 任务验证失败" in result_text, \
            f"结果流应含验证失败标记，实际: {result_text}"
        assert "数据库中无任务记录" in result_text, \
            f"结果流应含失败原因，实际: {result_text}"
        assert "reminder set" in result_text, \
            f"结果流应保留子 Agent 原始返回，实际: {result_text}"

        # system yield 保留（verbose 调试通道——3 既有测试锁定行为不回归）
        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        system_events = [e for e in stream_events if e.type == "system"]
        assert any("No task found" in e.content for e in system_events), \
            f"system yield 应保留 'No task found'，实际: {system_events}"

    def test_subagent_db_missing_fails_verification(self):
        """P3-2：event-manager 数据库文件缺失 → 验证失败可见化（不再静默通过）。

        第四分支补全（数据库错误/无任务/验证异常/数据库缺失）——system yield
        （验证失败类）+ display_result 注入失败原因（chat-with 结果流）。
        """
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create memory.json pointing to tmpdir——但【不创建】scheduled_tasks.db
            memory_path = Path(tmpdir) / ".niu" / "memory.json"
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text('{"workspace": {"path": "' + tmpdir + '"}}', encoding="utf-8")

            with patch("agent.runner.get_runner", return_value=mock_runner), \
                 patch("agent.subagent.call_subagent", return_value="reminder set"), \
                 patch.object(Path, "home", return_value=Path(tmpdir)):
                gen = handler._call_subagent_gen("event-manager", {"task": "提醒我明天开会"})
                events, return_value = _collect_events_with_return(gen)

        # system yield（验证失败类——原静默通过分支）
        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        system_events = [e for e in stream_events if e.type == "system"]
        assert any("数据库不存在，无法验证任务" in e.content for e in system_events), \
            f"应有包含 '数据库不存在' 的 system 事件，实际: {system_events}"

        # display_result 注入失败原因（主 Agent 下一轮可见）+ 原始返回保留
        assert return_value is not None, "应返回 StepOutcome"
        result_text = return_value.data["result"]
        assert "[event-manager 任务验证失败" in result_text, \
            f"结果流应含验证失败标记，实际: {result_text}"
        assert "数据库不存在，无法验证任务" in result_text, \
            f"结果流应含失败原因，实际: {result_text}"
        assert "reminder set" in result_text, \
            f"结果流应保留子 Agent 原始返回，实际: {result_text}"

    def test_subagent_verify_fail_reason_length_capped(self):
        """P3-1：验证失败原因长度上限——超长失败文本保尾截断（200 字符）。

        防异常文本（如超长数据库错误消息）挤占 tool_marker 200 截断窗口；
        截断保留尾部（信息最新端）且失败标记仍在结果流头部。
        """
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        import sqlite3 as real_sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            memory_path = Path(tmpdir) / ".niu" / "memory.json"
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text('{"workspace": {"path": "' + tmpdir + '"}}', encoding="utf-8")

            db_path = Path(tmpdir) / "scheduled_tasks.db"
            real_conn = real_sqlite3.connect(str(db_path))
            real_conn.execute(
                "CREATE TABLE IF NOT EXISTS scheduled_tasks ("
                "id INTEGER PRIMARY KEY, content TEXT, status TEXT, "
                "scheduled_at TEXT, created_at TEXT)"
            )
            real_conn.commit()
            real_conn.close()

            # 超长数据库错误消息（500 字符）→ verify_fail_reason 必须被截断
            with patch("agent.runner.get_runner", return_value=mock_runner), \
                 patch("agent.subagent.call_subagent", return_value="reminder set"), \
                 patch.object(Path, "home", return_value=Path(tmpdir)), \
                 patch.object(real_sqlite3, "connect", side_effect=real_sqlite3.Error("x" * 500)):
                gen = handler._call_subagent_gen("event-manager", {"task": "提醒我明天开会"})
                events, return_value = _collect_events_with_return(gen)

        assert return_value is not None, "应返回 StepOutcome"
        result_text = return_value.data["result"]
        assert "[event-manager 任务验证失败" in result_text, \
            f"结果流应含验证失败标记（头部——截断不应挤掉标记），实际: {result_text}"

        # 失败原因截断到 ≤200 字符（保尾——尾部信息保留）
        reason_part = result_text.split("任务验证失败：", 1)[1].split("]\n", 1)[0]
        assert len(reason_part) <= 200, \
            f"验证失败原因应 ≤200 字符，实际 {len(reason_part)}: {reason_part}"
        assert reason_part.endswith("x" * 10), \
            f"保尾截断应保留尾部信息，实际尾部: {reason_part[-20:]!r}"

        # system yield 保留（verbose 调试通道——原样未截断）
        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        system_events = [e for e in stream_events if e.type == "system"]
        assert any("Database error" in e.content for e in system_events), \
            f"system yield 应保留 Database error，实际: {system_events}"

    def test_subagent_verify_success_keeps_display_result_discard(self):
        """E4-11：event-manager 验证成功（DB 有任务）→ display_result 不注入（成功分支保持丢弃）。"""
        handler = _make_handler()
        mock_runner = Mock()
        mock_runner.llm_config = {"model": "test"}

        import sqlite3
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create memory.json pointing to tmpdir
            memory_path = Path(tmpdir) / ".niu" / "memory.json"
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text('{"workspace": {"path": "' + tmpdir + '"}}', encoding="utf-8")

            # Create scheduled_tasks.db with one row (verified task exists)
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
            conn.execute(
                "INSERT INTO scheduled_tasks (content, status, scheduled_at, created_at) "
                "VALUES ('开会提醒', 'pending', '2026-08-17 09:00:00', '2026-08-16 10:00:00')"
            )
            conn.commit()
            conn.close()

            with patch("agent.runner.get_runner", return_value=mock_runner), \
                 patch("agent.subagent.call_subagent", return_value="reminder set"), \
                 patch.object(Path, "home", return_value=Path(tmpdir)):
                gen = handler._call_subagent_gen("event-manager", {"task": "提醒我明天开会"})
                events, return_value = _collect_events_with_return(gen)

        # 验证成功 → 结果流保持子 Agent 原始返回（不注入失败文本）
        assert return_value is not None, "应返回 StepOutcome"
        assert return_value.data["result"] == "reminder set", \
            f"验证成功时结果流不应注入失败文本，实际: {return_value.data['result']}"
        assert "[event-manager 任务验证失败" not in return_value.data["result"]

        # 验证成功 → tool_marker 展示 Verified
        tool_markers = [
            e.content for e in events
            if isinstance(e, StreamEvent) and e.type == "tool_marker"
        ]
        assert any("Verified task in database" in c for c in tool_markers), \
            f"应展示 Verified tool_marker，实际: {tool_markers}"


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
        # dispatch bare-name auto-resolve 会迭代 registry._server_tools/_schemas（真实实现为 dict）——补全 mock 形状
        mock_registry._server_tools = {}
        mock_registry._schemas = {}
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
        # dispatch bare-name auto-resolve 会迭代 registry._server_tools/_schemas（真实实现为 dict）——补全 mock 形状
        mock_registry._server_tools = {}
        mock_registry._schemas = {}
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
        # dispatch bare-name auto-resolve 会迭代 registry._server_tools/_schemas（真实实现为 dict）——补全 mock 形状
        mock_registry._server_tools = {}
        mock_registry._schemas = {}
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
