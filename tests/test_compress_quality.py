"""压缩配置读取与截断检测测试（T6：压缩管线用例随退役删除，仅保留仍存活的配置读取与子 Agent 截断检测）。"""
from unittest.mock import patch

import niu_api.llm_proxy as llm_proxy_module
from agent.generic.llmcore import MockResponse
from agent.subagent import (
    _read_max_output_tokens,
)


def test_read_max_output_tokens_dynamic_calc():
    """max_output_tokens 动态算：contextWindowSize × 0.16，封顶 65536。

    不读配置 maxOutputTokens（已删除硬编码）。
    换模型自动适配：200K → 32000；128K → 20480；400K → 64000（封顶前）；500K → 65536（封顶）。
    """
    # mock _read_context_window_tokens 返回不同窗口大小
    with patch("agent.subagent._read_context_window_tokens", return_value=200000):
        assert _read_max_output_tokens() == 32000  # 200000 × 0.16

    with patch("agent.subagent._read_context_window_tokens", return_value=128000):
        assert _read_max_output_tokens() == 20480  # 128000 × 0.16

    with patch("agent.subagent._read_context_window_tokens", return_value=400000):
        assert _read_max_output_tokens() == 64000  # 400000 × 0.16 = 64000，未达封顶

    with patch("agent.subagent._read_context_window_tokens", return_value=500000):
        assert _read_max_output_tokens() == 65536  # 500000 × 0.16 = 80000，封顶 65536


def test_mock_response_has_finish_reason_default():
    """MockResponse 不传 finish_reason 时默认 None。"""
    resp = MockResponse(thinking="", content="hello", tool_calls=[], raw={}, stop_reason="end_turn")
    assert resp.finish_reason is None


def test_mock_response_has_finish_reason_set():
    """MockResponse 传 finish_reason 时能设置。"""
    resp = MockResponse(
        thinking="", content="hello", tool_calls=[], raw={}, stop_reason="end_turn",
        finish_reason="length"
    )
    assert resp.finish_reason == "length"


def test_litellm_adapter_finish_reason_from_stream(monkeypatch):
    """litellm_adapter 流式循环应捕获最后一个 chunk 的 finish_reason 传入 MockResponse。"""
    from types import SimpleNamespace

    from agent.generic.litellm_adapter import LiteLLMSession

    # 构造 fake chunk 流：3 个 chunk，最后一个 finish_reason='length'
    def make_chunk(content=None, finish_reason=None):
        delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
            usage=None,
        )

    fake_chunks = [
        make_chunk(content="hello"),
        make_chunk(content=" world"),
        make_chunk(finish_reason="length"),  # 最后一个 chunk 带 finish_reason
    ]

    # mock litellm.completion 返回 fake_chunks 迭代器
    import litellm
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: iter(fake_chunks))

    # LiteLLMSession 接收 cfg dict（不是关键字参数），见 BaseSession.__init__
    cfg = {
        "apikey": "test",
        "apibase": "http://test",
        "model": "test-model",
        "read_timeout": 30,
    }
    session = LiteLLMSession(cfg)
    messages = [{"role": "user", "content": "test"}]
    gen = session.chat(messages=messages, tools=None)
    # 消费生成器拿 MockResponse（通过 StopIteration.value）
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        result = e.value

    assert result is not None
    assert isinstance(result, MockResponse)
    assert result.finish_reason == "length"
    assert result.content.startswith("hello world")
    assert "[输出因超过最大长度被自动截断" in result.content


def test_agent_loop_return_value_contains_finish_reason(monkeypatch):
    """agent_runner_loop 正常完成（无工具调用）时 return_value 应含 response 的 finish_reason。"""
    # mock 停止标志，避免真实初始化 agent.runner
    # 注意：is_stop_requested/clear_stop/drain_supplement 在 agent_runner_loop 函数内部
    # 通过 `from agent.runner import ...` 导入，需 patch agent.runner 模块
    from agent import runner as _runner_mod
    from agent.generic import agent_loop
    from agent.generic.llmcore import MockResponse
    monkeypatch.setattr(_runner_mod, "is_stop_requested", lambda: False)
    monkeypatch.setattr(_runner_mod, "clear_stop", lambda: None)
    monkeypatch.setattr(_runner_mod, "drain_supplement", lambda: None)

    # mock 输出校验：永远返回 valid（避免 harness 重试逻辑干扰）
    class _FakeValidation:
        is_valid = True

        def format_feedback(self):  # pragma: no cover - 不会被调用
            return ""

    monkeypatch.setattr(agent_loop, "validate_references", lambda content: _FakeValidation())

    # mock 最小 handler：L281-284 需要 _last_prompt_tokens/_done_hooks/max_turns
    class _FakeHandler:
        _last_prompt_tokens = 0
        _done_hooks = []
        max_turns = 1
        current_turn = 1

        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    # mock LLM 客户端：chat 返回 generator，yield 文本 chunk，StopIteration 返回 MockResponse
    def _fake_chat(self, messages, tools=None, response_format=None):
        resp = MockResponse(
            thinking="",
            content="keep=1,2,3",
            tool_calls=[],
            raw="keep=1,2,3",
            finish_reason="stop",
        )
        yield "keep=1,2,3"
        return resp

    class _FakeClient:
        last_tools = ""

        def chat(self, messages, tools=None, response_format=None):
            return _fake_chat(self, messages, tools, response_format)

    gen = agent_loop.agent_runner_loop(
        client=_FakeClient(),
        system_prompt="test",
        user_input="test",
        handler=_FakeHandler(),
        tools_schema=[],
        max_turns=1,
        initial_user_content="test",
        enable_supplement=False,
    )

    return_value = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return_value = e.value

    assert return_value is not None
    assert isinstance(return_value, dict)
    assert return_value.get("result") == "CURRENT_TASK_DONE"
    assert return_value.get("finish_reason") == "stop"


def test_call_subagent_detects_truncation(monkeypatch):
    """call_subagent 检测 finish_reason=='length' 时返回 'COMPACT_TRUNCATED:' 前缀 + 截断内容。"""
    from agent import subagent

    # mock _run_agent_loop 返回 finish_reason='length' 的 return_value
    def fake_run_agent_loop(**kwargs):
        return "部分输出...", {"result": "CURRENT_TASK_DONE", "data": {}, "finish_reason": "length"}, ""

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)

    # call_subagent 内部 from .handler import NiuHandler / from .runner import create_client, get_tools_schema
    # 函数内 import 直接从源模块拿，必须 patch 源模块（不是 subagent 模块）
    import agent.handler as handler_module
    import agent.runner as runner_module
    class FakeClient:
        # subagent L979 会写 client.backend.stop_check（停止感知注入）
        class backend:
            stop_check = None
    monkeypatch.setattr(runner_module, "create_client", lambda cfg: FakeClient())
    monkeypatch.setattr(runner_module, "get_tools_schema", lambda **kwargs: [])
    # NiuHandler 需要支持 _disable_memory_recall / _is_subagent 属性赋值
    class FakeHandler:
        def __init__(self, mcp_client=None):
            self._disable_memory_recall = False
            self._is_subagent = False
    monkeypatch.setattr(handler_module, "NiuHandler", FakeHandler)

    result = subagent.call_subagent(
        agent_name="context-manager",
        task="test",
        llm_config={"model": "test"},
    )
    assert result.startswith("COMPACT_TRUNCATED:")


def test_call_subagent_normal_return(monkeypatch):
    """call_subagent 正常完成时返回 result_text。"""
    from agent import subagent

    def fake_run_agent_loop(**kwargs):
        return "keep=1,2,3\nupdate=", {"result": "CURRENT_TASK_DONE", "data": {}, "finish_reason": "stop"}, ""

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)

    import agent.handler as handler_module
    import agent.runner as runner_module
    class FakeClient:
        # subagent L979 会写 client.backend.stop_check（停止感知注入）
        class backend:
            stop_check = None
    monkeypatch.setattr(runner_module, "create_client", lambda cfg: FakeClient())
    monkeypatch.setattr(runner_module, "get_tools_schema", lambda **kwargs: [])
    class FakeHandler:
        def __init__(self, mcp_client=None):
            self._disable_memory_recall = False
            self._is_subagent = False
    monkeypatch.setattr(handler_module, "NiuHandler", FakeHandler)

    result = subagent.call_subagent(
        agent_name="context-manager",
        task="test",
        llm_config={"model": "test"},
    )
    assert "keep=1,2,3" in result


class FakeMsg:
    """模拟 Message 对象（compat.py 用 getattr(msg, 'id') 等访问）。"""
    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls or []
        self.tool_call_id = tool_call_id
