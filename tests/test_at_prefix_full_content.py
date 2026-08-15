"""T1 @ 整段传递单元测试（@niu-agent 完整 / 8000 上限 / @user 完整无上限 / @end 两形态 / @end >2000 写文档）。

方案 v2.5（2026-08-15 用户上限逻辑拍板）：
- @niu-agent 进主 Agent 上下文必须保护——上限 8000（判定对象 = 完整 stripped）
- @user 给用户看——用户无上下文限制——无上限
- @end 前 + @end 后整段保留（标记剥掉）——>2000 写文档逻辑保持不改——写文档内容 = 新完整 exit_content
"""
from unittest import mock


def test_at_niu_full_content_passed_through(monkeypatch):
    """@niu-agent 整段传递：@ 前上下文 + @niu-agent + @ 后提问全部传给主 Agent（含标记原样保留）。"""
    from agent import subagent
    from agent.generic import agent_loop

    monkeypatch.setattr(
        subagent, "_ask_main_agent_impl",
        mock.Mock(return_value="主 Agent 的回答")
    )

    messages = [{"role": "system", "content": "你是子 Agent"}]
    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = False

    content = "Hacker News 今日热点：AI 芯片公司股价大涨……\n@niu-agent 请确认这份报告是否需要补充发布"
    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.INTERCEPTED, None)
    subagent._ask_main_agent_impl.assert_called_once()
    question = subagent._ask_main_agent_impl.call_args.kwargs["question"]
    assert question == content  # 完整 content——@ 前上下文 + @niu-agent + @ 后提问全传
    # messages 注入：assistant content（原始 content）+ 主 Agent 回答
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["content"] == content
    assert "主 Agent 的回答" in messages[-1]["content"]


def test_at_niu_over_8000_returns_format_error(monkeypatch):
    """@niu-agent 完整内容超过 8000 字符 → FORMAT_ERROR（主 Agent 上下文保护）——文案同步 8000。"""
    from agent import subagent
    from agent.generic import agent_loop

    monkeypatch.setattr(
        subagent, "_ask_main_agent_impl",
        mock.Mock(return_value="不应被调用")
    )
    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = False
    messages = [{"role": "user", "content": "开始"}]

    content = "上下文" * 3000 + "@niu-agent 问题"  # 9000+ 字符 > 8000
    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.FORMAT_ERROR, None)
    assert "8000" in messages[-1]["content"]  # 动态文案硬编码 2000 同步改 8000
    subagent._ask_main_agent_impl.assert_not_called()


def test_at_niu_exactly_8000_ok(monkeypatch):
    """@niu-agent 完整内容恰好 8000 字符 → 放行不误判（len(question) > 8000 语义——精确边界）。"""
    from agent import subagent
    from agent.generic import agent_loop

    monkeypatch.setattr(
        subagent, "_ask_main_agent_impl",
        mock.Mock(return_value="回答")
    )
    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = False
    messages = [{"role": "user", "content": "开始"}]

    content = "a" * 7987 + "@niu-agent 问题"  # 7987 + 13 = 恰好 8000
    assert len(content) == 8000
    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.INTERCEPTED, None)
    assert subagent._ask_main_agent_impl.call_args.kwargs["question"] == content


def test_at_niu_under_8000_ok_with_full_content(monkeypatch):
    """@niu-agent 完整内容在 8000 内（含 @ 前长上下文）→ 正常传递不误判。"""
    from agent import subagent
    from agent.generic import agent_loop

    monkeypatch.setattr(
        subagent, "_ask_main_agent_impl",
        mock.Mock(return_value="回答")
    )
    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = False
    messages = [{"role": "user", "content": "开始"}]

    # 长上下文放行场景——整段传必须通过
    # （P3 修正：原注释误称"2325 字符 HN 报告场景（Bug 1 实证规模）"——实构 400×4+19 ≈ 1619 字符）
    content = "报告内容" * 400 + "\n@niu-agent 以上报告请查收"  # ~1620 字符 < 8000
    assert len(content) < 8000
    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.INTERCEPTED, None)
    assert subagent._ask_main_agent_impl.call_args.kwargs["question"] == content


def test_at_user_full_content_no_limit(monkeypatch):
    """@user 完整 content 传递——无上限（超长整段也完整传——用户无上下文限制）。"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = True
    messages = [{"role": "user", "content": "开始"}]

    # 远超 8000 字符（旧上限也远超）——@user 无上限必须完整传
    content = "背景信息" * 3000 + "@user 请确认这份文件是否需要继续处理"
    assert len(content) > 8000
    result = agent_loop._intercept_at_prefix_content(
        content=content,
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )

    assert result == (agent_loop.INTERCEPTED_ASK_USER, content)  # payload = 完整 content


def test_at_user_empty_after_marker_returns_format_error(monkeypatch):
    """@user 标记后无内容 → 仍 FORMAT_ERROR（空问题守卫保留——裸 @user 即时纠错）。"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = True
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="前面上下文 @user",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )

    assert result == (agent_loop.FORMAT_ERROR, None)
    assert "对话格式错误" in messages[-1]["content"]


def test_at_niu_empty_after_marker_returns_format_error(monkeypatch):
    """@niu-agent 标记后无内容 → 仍 FORMAT_ERROR（空问题守卫保留——与整段传递并存）。"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = False
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="前面上下文 @niu-agent",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=mock.MagicMock(),
    )

    assert result == (agent_loop.FORMAT_ERROR, None)
    assert "对话格式错误" in messages[-1]["content"]


def test_at_user_word_boundary_not_intercepted():
    """@user 后面紧跟非空白字符（如 @username）→ 不拦截（词边界回归——@username 不拦）。"""
    from agent.generic import agent_loop

    fake_handler = mock.MagicMock()
    fake_handler._subagent_unique_name = "test-agent-abc1"
    fake_handler._is_sync_subagent = True
    messages = [{"role": "user", "content": "开始"}]

    result = agent_loop._intercept_at_prefix_content(
        content="@username 是子 Agent 吗",
        tool_calls=[],
        messages=messages,
        handler=fake_handler,
        memory_context=None,
    )

    # 词边界失败 → 不拦截 → 落入格式错误分支
    assert result == (agent_loop.FORMAT_ERROR, None)
    assert "对话格式错误" in messages[-1]["content"]


def test_compute_exit_content_end_at_end_form():
    """@end 两形态之一：@end 在末尾 → 前内容完整保留（标记剥掉 + 尾部空白归一——无尾随空格）。"""
    from agent.generic.agent_loop import _compute_exit_content

    content = "汇报正文内容 @end"
    stripped = content.lstrip()
    idx = stripped.index("@end")
    assert _compute_exit_content(stripped, idx, content) == "汇报正文内容"


def test_compute_exit_content_end_in_middle_form():
    """@end 两形态之二：@end 在中间 → 前 + 后拼接（标记剥掉 + 段间空白归一为单空格——前半不再丢弃）。"""
    from agent.generic.agent_loop import _compute_exit_content

    content = "前段汇报 @end 后段残留说明"
    stripped = content.lstrip()
    idx = stripped.index("@end")
    assert _compute_exit_content(stripped, idx, content) == "前段汇报 后段残留说明"


def test_compute_exit_content_end_whitespace_only_falls_back():
    """@end 后仅空白（如 "@end\\n"——LLM 常见尾随换行）→ 拼接结果纯空白也兜底原始 content（P3 修复：strip 后再判空）。"""
    from agent.generic.agent_loop import _compute_exit_content

    content = "@end\n"
    stripped = content.lstrip()
    idx = stripped.index("@end")
    assert _compute_exit_content(stripped, idx, content) == content


def test_compute_exit_content_end_leading_marker_form():
    """@end 在开头 + 后有内容 → 前段空 + 后段拼接（前导空白归一——无前导空格）。"""
    from agent.generic.agent_loop import _compute_exit_content

    content = "@end 后续残留说明"
    stripped = content.lstrip()
    idx = stripped.index("@end")
    assert _compute_exit_content(stripped, idx, content) == "后续残留说明"


def test_at_end_over_2000_writes_file_with_full_content(tmp_path, monkeypatch):
    """@end 中间 + 总长 >2000 → 写文档内容 = 前+后完整拼接值（防丢一半回归——R3-A P3-3）。"""
    import glob

    from agent.generic import agent_loop
    from agent.generic.llmcore import MockResponse

    prefix = "第一部分报告内容。" * 150  # ~ 1200 字符
    suffix = "第二部分后续内容。" * 150  # ~ 1200 字符
    content = prefix + "@end" + suffix  # @end 在中间，总长 > 2000
    assert len(content) > 2000

    class _Handler:
        _is_subagent = True
        _is_sync_subagent = True
        _subagent_unique_name = "test-agent"
        _program_triggered = False
        _current_messages = []
        current_turn = 0
        _last_prompt_tokens = 0
        last_tools = ""
        _done_hooks = []

        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

        def tool_before_callback(self, tool_name, args, response):
            pass

        def tool_after_callback(self, tool_name, args, response, ret):
            pass

    class _Client:
        def __init__(self, content):
            self.chat_called = 0
            self.last_tools = ""
            self._content = content

        def chat(self, messages, tools=None):
            self.chat_called += 1
            resp = MockResponse(
                thinking="", content=self._content, tool_calls=[], raw=self._content,
                finish_reason="end_turn", usage={},
            )

            def _gen():
                yield from ()
                return resp

            return _gen()

    monkeypatch.setattr(agent_loop, "get_tmp_dir", lambda: tmp_path)
    handler = _Handler()
    client = _Client(content)

    events = []
    gen = agent_loop.agent_runner_loop(
        client=client,
        system_prompt="你是子 Agent",
        user_input="任务",
        handler=handler,
        tools_schema=[],
        verbose=False,
        enable_supplement=False,
        max_turns=5,
    )
    with mock.patch("agent.runner.is_stop_requested", return_value=False):
        while True:
            try:
                events.append(next(gen))
            except StopIteration as e:
                rv = e.value
                break

    assert rv["result"] == "EXITED"
    replies = [ev.content for ev in events if ev.type == "reply"]
    # 第一个 reply = 完整拼接内容（@end 标记剥掉 + 段间空白归一为单空格——P3）；
    # 第二个 reply = 文件路径提示
    assert replies[0] == prefix + " " + suffix
    assert len(replies) >= 2
    assert "已超限" in replies[1]

    # 写文档内容 = 归一化后的前+后完整拼接值（不丢一半）
    written = glob.glob(str(tmp_path / "*.md"))
    assert len(written) == 1
    file_content = open(written[0], encoding="utf-8").read()
    assert file_content == prefix + " " + suffix
