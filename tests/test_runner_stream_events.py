"""测试 runner.py 的 chat() 方法正确消费 StreamEvent。

TDD: 先写测试，确认失败，再改代码。

核心验证点：
1. chat() 只 yield type="reply" 的内容（SSE 管道）
2. full_resp 只累积 reply 内容（DB 管道）
3. 向后兼容普通 str chunk
4. _clean_stream_output 已被删除
"""
import pathlib
from unittest.mock import Mock, patch

from agent.generic.agent_loop import StreamEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_runner():
    """创建一个 NiuRunner 实例（mock 掉所有重依赖）。"""
    with patch("agent.runner.create_client") as mock_create_client, \
         patch("agent.runner.get_system_prompt", return_value="sys"), \
         patch("agent.runner.get_tools_schema", return_value=[]), \
         patch("agent.runner.get_skill_sync"), \
         patch("agent.runner.NiuHandler"), \
         patch("niu_api.internal.disk_engine.DiskEngine") as mock_disk_cls:
        mock_create_client.return_value = Mock()
        mock_disk_instance = Mock()
        mock_disk_instance.get_schema.return_value = {"type": "function", "function": {"name": "disk"}}
        mock_disk_instance.config.servers = {}  # empty servers dict for _build_disk_description
        mock_disk_cls.return_value = mock_disk_instance

        from agent.runner import NiuRunner
        runner = NiuRunner(
            llm_config={"apikey": "test", "model": "test-model"},
            mcp_client=None,
        )
    return runner


# ---------------------------------------------------------------------------
# 测试 1: chat() 只 yield type="reply" 的内容
# ---------------------------------------------------------------------------

def test_chat_yields_only_reply_content():
    """chat() 只应 yield type='reply' 的内容，过滤 system 和 tool_marker。"""
    runner = _make_runner()

    events = [
        StreamEvent("system", "LLM Running...\n"),
        StreamEvent("tool_marker", "[MCP] tool executed\n"),
        StreamEvent("reply", "Hello "),
        StreamEvent("reply", "world"),
        StreamEvent("system", "some system msg"),
    ]

    with patch("agent.runner.agent_runner_loop", return_value=iter(events)):
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    # 只有 reply 内容被 yield
    assert results == ["Hello ", "world"]


# ---------------------------------------------------------------------------
# 测试 2: chat() 的 full_resp 只包含 reply 内容（用于DB存储）
# ---------------------------------------------------------------------------

def test_chat_full_resp_only_reply():
    """chat() 内部 full_resp 只含 reply 内容，不含 system/tool_marker。

    验证方式：chat() 最终 yield 的拼接结果应等于所有 reply 内容的拼接。
    """
    runner = _make_runner()

    events = [
        StreamEvent("system", "LLM Running...\n"),
        StreamEvent("reply", "Hello "),
        StreamEvent("tool_marker", "[MCP] tool\n"),
        StreamEvent("reply", "world"),
    ]

    with patch("agent.runner.agent_runner_loop", return_value=iter(events)):
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    full_resp = "".join(results)
    assert full_resp == "Hello world"
    assert "LLM Running" not in full_resp
    assert "[MCP]" not in full_resp


# ---------------------------------------------------------------------------
# 测试 3: chat() 向后兼容普通 str chunk
# ---------------------------------------------------------------------------

def test_chat_backward_compat_str():
    """chat() 向后兼容普通 str chunk（非 StreamEvent）。"""
    runner = _make_runner()

    events = ["Hello ", "world"]

    with patch("agent.runner.agent_runner_loop", return_value=iter(events)):
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    assert results == ["Hello ", "world"]


# ---------------------------------------------------------------------------
# 测试 4: _clean_stream_output 应该已被删除
# ---------------------------------------------------------------------------

def test_no_clean_stream_output():
    """_clean_stream_output 应该已被删除 — 双管道架构替代了正则补丁。"""
    source = pathlib.Path("REDACTED_USER_PATH/tools/ai-bot/agent/runner.py").read_text()
    assert "_clean_stream_output" not in source, \
        "_clean_stream_output should be removed — dual pipeline replaces it"


# ---------------------------------------------------------------------------
# 测试 5: chat() 混合 StreamEvent 和 str 时正确处理
# ---------------------------------------------------------------------------

def test_chat_mixed_stream_event_and_str():
    """chat() 同时处理 StreamEvent 和 str chunk。"""
    runner = _make_runner()

    events = [
        StreamEvent("system", "system msg"),
        "plain str chunk",
        StreamEvent("reply", "reply content"),
        StreamEvent("tool_marker", "tool marker"),
    ]

    with patch("agent.runner.agent_runner_loop", return_value=iter(events)):
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    assert results == ["plain str chunk", "reply content"]


# ---------------------------------------------------------------------------
# 测试 6: chat() 空 reply 内容不被 yield
# ---------------------------------------------------------------------------

def test_chat_empty_reply_not_yielded():
    """chat() 不 yield 空 reply 内容。"""
    runner = _make_runner()

    events = [
        StreamEvent("reply", ""),
        StreamEvent("reply", "Hello"),
        StreamEvent("reply", ""),
    ]

    with patch("agent.runner.agent_runner_loop", return_value=iter(events)):
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    # 空 reply 不应被 yield
    assert results == ["Hello"]


# ---------------------------------------------------------------------------
# 测试 7: chat() 保留 StopIteration return_value
# ---------------------------------------------------------------------------

def test_chat_preserves_return_value():
    """chat() 应保留 agent_runner_loop 的 return_value（用于 CONTEXT_OVERFLOW 检测）。"""
    runner = _make_runner()

    def gen_with_return():
        yield StreamEvent("reply", "response")
        return {"result": "CONTEXT_OVERFLOW", "data": {"tokens_used": 150000}}

    with patch("agent.runner.agent_runner_loop", return_value=gen_with_return()):
        results = list(runner.chat(
            session_id="test-session",
            user_input="test",
        ))

    assert results == ["response"]
    assert runner.last_return_value is not None
    assert runner.last_return_value.get("result") == "CONTEXT_OVERFLOW"
