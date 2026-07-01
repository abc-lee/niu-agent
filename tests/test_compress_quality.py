"""context-manager 压缩质量修复测试。"""
import json
from unittest.mock import patch

from agent.generic.llmcore import MockResponse
from agent.subagent import (
    _read_compress_target_tokens,
    _read_max_output_tokens,
)
from niu_api.compat import _strip_analysis


def test_read_compress_target_tokens_default(tmp_path):
    """配置无 compressTargetTokens 时返回默认 60000。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"context": {}}))
    with patch("agent.subagent._get_user_config_path", return_value=config_file):
        assert _read_compress_target_tokens() == 60000


def test_read_compress_target_tokens_custom(tmp_path):
    """配置有 compressTargetTokens 时返回自定义值。"""
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps({"context": {"compressTargetTokens": 80000}}))
    with patch("agent.subagent._get_user_config_path", return_value=config_file):
        assert _read_compress_target_tokens() == 80000


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


def test_read_compress_target_tokens_invalid_returns_default(tmp_path):
    """配置 compressTargetTokens 为非法值（0/负数/字符串/bool）时返回默认 60000。"""
    config_file = tmp_path / "config.json"
    for invalid_val in [0, -100, "60000", True, None]:
        config_file.write_text(json.dumps({"context": {"compressTargetTokens": invalid_val}}))
        with patch("agent.subagent._get_user_config_path", return_value=config_file):
            assert _read_compress_target_tokens() == 60000, f"非法值 {invalid_val!r} 应返回默认 60000"


def test_strip_analysis_closed():
    """闭合的 <analysis>...</analysis> 块被剥离。"""
    raw = "<analysis>\n第一份 idx 1-100\n</analysis>\n\nkeep=1,2,3\nupdate=1|摘要"
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,2,3" in result
    assert "update=1|摘要" in result
    assert "第一份" not in result


def test_strip_analysis_unclosed():
    """未闭合的 <analysis>（有开始无结束）被剥离到字符串末尾。"""
    raw = "<analysis>\n第一份 idx 1-100\nkeep=1,2,3"
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,2,3" not in result  # 未闭合时 keep= 在 analysis 块里被一起剥离


def test_strip_analysis_case_insensitive():
    """大小写不敏感：<ANALYSIS> 也能剥离。"""
    raw = "<ANALYSIS>\n分析内容\n</ANALYSIS>\n\nkeep=1,2,3"
    result = _strip_analysis(raw)
    assert "<ANALYSIS>" not in result.lower()
    assert "keep=1,2,3" in result


def test_strip_analysis_missing():
    """没有 <analysis> 块时原样返回。"""
    raw = "keep=1,2,3\nupdate=1|摘要"
    result = _strip_analysis(raw)
    assert result == raw


def test_strip_analysis_multiline():
    """analysis 块跨多行（含换行）被完整剥离。"""
    raw = """<analysis>
第一份 idx 1-100：含 3 个会话单元
第二份 idx 101-200：估算释放 3K
累计 11K，已达目标
</analysis>

keep=1,5,15,30
update=1|[摘要] 智能家居;5|[摘要] 知识图谱
cursor=30"""
    result = _strip_analysis(raw)
    assert "<analysis>" not in result
    assert "keep=1,5,15,30" in result
    assert "cursor=30" in result
    assert "会话单元" not in result


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
    from agent.generic.litellm_adapter import LiteLLMSession
    from types import SimpleNamespace

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
    assert result.content == "hello world"


def test_agent_loop_return_value_contains_finish_reason(monkeypatch):
    """agent_runner_loop 正常完成（无工具调用）时 return_value 应含 response 的 finish_reason。"""
    from agent.generic import agent_loop
    from agent.generic.llmcore import MockResponse

    # mock 停止标志，避免真实初始化 agent.runner
    # 注意：is_stop_requested/clear_stop/drain_supplement 在 agent_runner_loop 函数内部
    # 通过 `from agent.runner import ...` 导入，需 patch agent.runner 模块
    from agent import runner as _runner_mod
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
            finish_reason="length",
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
    assert return_value.get("finish_reason") == "length"


def test_call_subagent_detects_truncation(monkeypatch):
    """call_subagent 检测 finish_reason=='length' 时返回 'COMPACT_TRUNCATED'。"""
    from agent import subagent

    # mock _run_agent_loop 返回 finish_reason='length' 的 return_value
    def fake_run_agent_loop(**kwargs):
        return "部分输出...", {"result": "CURRENT_TASK_DONE", "data": {}, "finish_reason": "length"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)

    # call_subagent 内部 from .handler import NiuHandler / from .runner import create_client, get_tools_schema
    # 函数内 import 直接从源模块拿，必须 patch 源模块（不是 subagent 模块）
    import agent.handler as handler_module
    import agent.runner as runner_module
    class FakeClient:
        pass
    monkeypatch.setattr(runner_module, "create_client", lambda cfg: FakeClient())
    monkeypatch.setattr(runner_module, "get_tools_schema", lambda: [])
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
    assert result == "COMPACT_TRUNCATED"


def test_call_subagent_normal_return(monkeypatch):
    """call_subagent 正常完成时返回 result_text。"""
    from agent import subagent

    def fake_run_agent_loop(**kwargs):
        return "keep=1,2,3\nupdate=", {"result": "CURRENT_TASK_DONE", "data": {}, "finish_reason": "stop"}

    monkeypatch.setattr(subagent, "_run_agent_loop", fake_run_agent_loop)

    import agent.handler as handler_module
    import agent.runner as runner_module
    class FakeClient:
        pass
    monkeypatch.setattr(runner_module, "create_client", lambda cfg: FakeClient())
    monkeypatch.setattr(runner_module, "get_tools_schema", lambda: [])
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


def test_mode2_prompt_contains_methodology(monkeypatch):
    """模式二 task prompt 应含压缩方法论（三份/会话单元/硬约束）+ llm_config 注入 max_tokens。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 120000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    captured = {}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            captured["task"] = kwargs.get("task", "")
            captured["history"] = kwargs.get("history")
            captured["llm_config"] = kwargs.get("llm_config", {})
            return "<analysis>分析</analysis>\nkeep=1,2\nupdate="
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 16384, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    # 不用 try/except 吞异常
    asyncio.run(compat._tidy_context_impl(request))

    # 验证 call_subagent 被调用且捕获了参数
    assert "task" in captured, "call_subagent 未被调用，可能 _tidy_context_impl 提前返回或抛错"
    # prompt 含方法论关键词
    assert "压缩方法论" in captured["task"]
    assert "第一份" in captured["task"]
    assert "会话单元" in captured["task"]
    assert "<analysis>" in captured["task"]
    # llm_config 注入了 max_tokens
    assert captured["llm_config"].get("litellm_kwargs", {}).get("max_tokens") == 16384


def test_mode2_truncate_triggers_emergency_clear(monkeypatch):
    """模式二 LLM 输出截断时触发应急清空（保留最近 10 条，上面全删，最旧改摘要）。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    # 15 条消息，截断时应保留最近 10 条，删前 5 条，第 6 条改摘要
    messages = [FakeMsg(id=f"msg-{i}", role="user", content=f"内容{i}") for i in range(1, 16)]

    deleted_ids = []
    updated_ids = []
    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages
        async def delete_messages_by_ids(self, message_ids):
            deleted_ids.extend(message_ids)
            return {"deleted_count": len(message_ids), "freed_tokens": 0}
        async def update_message(self, message_id=None, content=None, **kw):
            updated_ids.append((message_id, content))
            return True

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 120000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    # LLM 返回 COMPACT_TRUNCATED（截断）
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            return "COMPACT_TRUNCATED"
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    result = asyncio.run(compat._tidy_context_impl(request))

    # 验证返回 skipped + emergency cleared
    assert result is not None
    assert result.get("status") == "skipped"
    assert "emergency cleared" in result.get("reason", "")
    # 验证删了前 5 条（msg-1 到 msg-5）
    assert len(deleted_ids) == 5
    assert "msg-1" in deleted_ids
    assert "msg-5" in deleted_ids
    # 验证第 6 条（msg-6，保留区最旧）被改为摘要
    assert len(updated_ids) == 1
    assert updated_ids[0][0] == "msg-6"
    assert "压缩失败" in updated_ids[0][1]


def test_mode2_truncate_too_few_no_clear(monkeypatch):
    """模式二截断但历史不足 10 条时，不删不改，返回 skipped no clear needed。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    # 只有 3 条消息，不足 10 条，不应删任何消息
    messages = [FakeMsg(id=f"msg-{i}", role="user", content=f"内容{i}") for i in range(1, 4)]

    deleted_ids = []
    updated_ids = []
    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages
        async def delete_messages_by_ids(self, message_ids):
            deleted_ids.extend(message_ids)
            return {"deleted_count": len(message_ids), "freed_tokens": 0}
        async def update_message(self, message_id=None, content=None, **kw):
            updated_ids.append((message_id, content))
            return True

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 120000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            return "COMPACT_TRUNCATED"
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    result = asyncio.run(compat._tidy_context_impl(request))

    # 验证返回 skipped + no clear needed
    assert result is not None
    assert result.get("status") == "skipped"
    assert "no clear needed" in result.get("reason", "")
    # 不删不改
    assert len(deleted_ids) == 0
    assert len(updated_ids) == 0


def test_strip_analysis_missing_then_parse():
    """LLM 没写 <analysis> 块时，_strip_analysis 原样返回，解析正常。"""
    from niu_api.compat import _strip_analysis

    # LLM 直接输出 keep/update，无 analysis 块
    raw = "keep=1,2,3\nupdate=1|摘要"
    result = _strip_analysis(raw)
    # 原样返回
    assert result == raw
    # 解析 keep/update 仍可用
    lines = result.strip().splitlines()
    keep_line = [l for l in lines if l.lower().startswith("keep=")]
    assert len(keep_line) == 1
    assert "1,2,3" in keep_line[0]


def test_mode3_prompt_contains_methodology(monkeypatch):
    """模式三 task prompt 应含压缩方法论 + cursor + dream 安全边界。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好，我是 Niu"),
    ]

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    captured = {}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            captured["task"] = kwargs.get("task", "")
            captured["llm_config"] = kwargs.get("llm_config", {})
            return "<analysis>分析</analysis>\nkeep=1,2\ncursor=2\nupdate="
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)

    request = {"session_id": "test", "mode": "force"}
    asyncio.run(compat._tidy_context_impl(request))

    assert "task" in captured, "call_subagent 未被调用"
    assert "压缩方法论" in captured["task"]
    assert "第一份" in captured["task"]
    assert "会话单元" in captured["task"]
    assert "<analysis>" in captured["task"]
    assert "cursor=" in captured["task"]
    assert "安全边界" in captured["task"]
    assert captured["llm_config"].get("litellm_kwargs", {}).get("max_tokens") == 32000


def test_mode3_truncate_triggers_emergency_clear(monkeypatch):
    """模式三 LLM 输出截断时触发应急清空（mode='force'）。"""
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    # 15 条消息，截断时应保留最近 10 条，删前 5 条，第 6 条改摘要
    messages = [FakeMsg(id=f"msg-{i}", role="user", content=f"内容{i}") for i in range(1, 16)]

    deleted_ids = []
    updated_ids = []
    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages
        async def delete_messages_by_ids(self, message_ids):
            deleted_ids.extend(message_ids)
            return len(message_ids)
        async def update_message(self, message_id=None, content=None, **kw):
            updated_ids.append((message_id, content))
            return True

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 180000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            return "COMPACT_TRUNCATED"
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)

    request = {"session_id": "test", "mode": "force"}
    result = asyncio.run(compat._tidy_context_impl(request))

    # 验证返回 skipped + emergency cleared + mode=force
    assert result is not None
    assert result.get("status") == "skipped"
    assert result.get("mode") == "force"
    assert "emergency cleared" in result.get("reason", "")
    # 验证删了前 5 条
    assert len(deleted_ids) == 5
    # 验证第 6 条被改为摘要
    assert len(updated_ids) == 1
    assert "压缩失败" in updated_ids[0][1]


def test_mode2_no_auto_keep_fixup(monkeypatch):
    """模式二不再自动把 update idx 补进 keep（keep 列表保持 LLM 原样）。

    LLM 回 keep=1（不含 update 的 idx 3），update=3|摘要（idx 3 不在 keep）。
    删除 auto-fixup 后：keep 只有 1，update 的 idx 3 进 overlap 处理（从 deletes
    移除），最终 msg-3 被 update 保留改摘要（不被删除）。

    注：测试用 4 条消息，让 entity/dream 游标自动推进到 msg-4（而非 msg-3），
    避免 cursor_ids_set 保护机制把 msg-3 的 update 误丢（与 auto-fixup 无关）。
    """
    import asyncio
    import niu_api.compat as compat
    import niu_api.chat as chat_module
    import agent.subagent as subagent_module

    messages = [
        FakeMsg(id="msg-1", role="user", content="你好"),
        FakeMsg(id="msg-2", role="assistant", content="你好"),
        FakeMsg(id="msg-3", role="user", content="测试"),
        FakeMsg(id="msg-4", role="assistant", content="收到"),
    ]

    deleted_ids = []
    updated_ids = []

    class FakeStore:
        async def get_messages(self, limit=None, before_id=None):
            return messages
        async def delete_messages_by_ids(self, message_ids):
            deleted_ids.extend(message_ids)
            return {"deleted_count": len(message_ids), "freed_tokens": 0}
        async def update_message(self, message_id=None, content=None, **kw):
            updated_ids.append(message_id)
            return True

    async def fake_get_message_store():
        return FakeStore()

    class FakeRunner:
        handler = type("H", (), {"_last_prompt_tokens": 120000})()
        llm_config = {}

    def fake_get_or_create_runner():
        return FakeRunner()

    # LLM 回 keep=1（不含 update 的 idx 3），update=3|摘要
    captured = {}
    def fake_call_subagent(*args, **kwargs):
        agent_name = kwargs.get("agent_name") or (args[0] if args else "")
        if agent_name == "context-manager":
            captured["task"] = kwargs.get("task", "")
            return "<analysis>分析</analysis>\nkeep=1\nupdate=3|摘要内容"
        return "skip"

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(chat_module, "get_or_create_runner", fake_get_or_create_runner)
    monkeypatch.setattr(subagent_module, "call_subagent", fake_call_subagent)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 200000, raising=False)
    monkeypatch.setattr(compat, "_read_warning_threshold", lambda: 0.8, raising=False)
    monkeypatch.setattr(compat, "_read_protect_recent_count", lambda: 0, raising=False)
    monkeypatch.setattr(compat, "_write_cursor_with_lock", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(compat, "_read_compress_target_tokens", lambda: 60000, raising=False)
    monkeypatch.setattr(compat, "_read_max_output_tokens", lambda: 32000, raising=False)

    request = {"session_id": "test", "mode": "sleep"}
    asyncio.run(compat._tidy_context_impl(request))

    # 验证 auto-fixup 已删除：
    # - msg-3 进 update（LLM 回 update=3），被 update 保留改摘要（overlap 从 deletes 移除）
    # - msg-3 不进 delete（overlap 兜底）
    assert "msg-3" in updated_ids, "msg-3 应被 update 保留改摘要"
    assert "msg-3" not in deleted_ids, "msg-3 不应被删除（overlap 从 deletes 移除）"
    # msg-2 既不在 keep 也不在 update，应被删除
    assert "msg-2" in deleted_ids, "msg-2 应被删除（不在 keep 也不在 update）"
