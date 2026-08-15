"""E2 Task 2：notify_llm_error_sync + compat 接线 + 源头友好化 + error_type 透传链测试。

覆盖：notify_llm_error_sync 事件格式/广播/早退/RuntimeError 守卫 / compat chat_session
LLM_ERROR 分支 skip persist + message_id=None 正常返回 + notify 友好文案 / chat_error
保留异常对象后 is_litellm_error_type 判定 / rv=None Stop 路径守卫 / 非 LLM 异常不误标 /
error_type 透传链全链路一致（adapter 记录 → MockResponse.error_type_name → agent_loop
两分支 yield + dict error_type） / 覆盖点同步（重试耗尽/socket fallback 末错类型对应末错文本） /
happy path MockResponse 构造无 NameError。
"""
import asyncio
import litellm
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import niu_api.chat as chat_mod
from agent.generic.llmcore import MockResponse

_QUOTA_FRIENDLY = "模型配额已用完，请等待配额恢复或更换模型"  # 通道 1 BudgetExceededError 文案（产品文案锁定）
_RATE_LIMIT_FRIENDLY = "模型服务限流（429），请稍后重试"  # 通道 1 RateLimitError 文案


# ===== notify_llm_error_sync =====

class _FakeLoop:
    """记录 call_soon_threadsafe 调用的假事件循环（不真正调度）。"""

    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, cb, *args):
        self.calls.append((cb, args))


def test_notify_llm_error_sync_broadcasts_event_to_subscribers():
    """事件格式（type/error_type/error_msg/source）经 _sync_broadcast 广播到订阅者队列。"""
    fake = _FakeLoop()
    old_loop = chat_mod._main_loop
    chat_mod._main_loop = fake
    chat_mod._event_subscribers.clear()
    q = asyncio.Queue()
    chat_mod._event_subscribers.append(q)
    try:
        chat_mod.notify_llm_error_sync("RateLimitError", _RATE_LIMIT_FRIENDLY, "chat_session")
        assert len(fake.calls) == 1
        cb, args = fake.calls[0]
        assert cb is chat_mod._sync_broadcast
        expected = {
            "type": "llm_error",
            "error_type": "RateLimitError",
            "error_msg": _RATE_LIMIT_FRIENDLY,
            "source": "chat_session",
        }
        assert args[0] == expected
        # 实际执行广播 → 事件进入订阅者队列
        cb(*args)
        assert not q.empty()
        assert q.get_nowait() == expected
    finally:
        chat_mod._main_loop = old_loop
        chat_mod._event_subscribers.clear()


def test_notify_llm_error_sync_none_type_passthrough():
    """error_type=None（通道 3）事件仍广播——error_type 为信息字段。"""
    fake = _FakeLoop()
    old_loop = chat_mod._main_loop
    chat_mod._main_loop = fake
    try:
        chat_mod.notify_llm_error_sync(None, "raw error text", "chat_queue")
        cb, args = fake.calls[0]
        assert args[0]["type"] == "llm_error"
        assert args[0]["error_type"] is None
        assert args[0]["error_msg"] == "raw error text"
        assert args[0]["source"] == "chat_queue"
    finally:
        chat_mod._main_loop = old_loop


def test_notify_llm_error_sync_main_loop_none_early_exit():
    """_main_loop=None 早退不抛。"""
    old_loop = chat_mod._main_loop
    chat_mod._main_loop = None
    try:
        chat_mod.notify_llm_error_sync("RateLimitError", "msg", "chat_session")
    finally:
        chat_mod._main_loop = old_loop


def test_notify_llm_error_sync_loop_closed_runtime_error_ignored():
    """loop 已关闭 call_soon_threadsafe 抛 RuntimeError → 不抛（守卫吞掉）。"""

    class _ClosedLoop:
        def call_soon_threadsafe(self, cb, *args):
            raise RuntimeError("Event loop is closed")

    old_loop = chat_mod._main_loop
    chat_mod._main_loop = _ClosedLoop()
    try:
        chat_mod.notify_llm_error_sync("RateLimitError", "msg", "chat_session")
    finally:
        chat_mod._main_loop = old_loop


# ===== compat chat_session 接线（mock 全链路，不跑真实 LLM） =====

def _mock_chat_session_env(runner):
    """搭 chat_session 运行环境：假锁/假 store/假 context_manager，返回可复用的 patch 上下文。

    runner 需注入 chat 行为与 last_return_value；本函数返回 (store, patches_ctx)。
    """
    from niu_api import compat

    fake_lock = MagicMock()
    fake_lock.locked.return_value = False
    fake_lock.acquire = AsyncMock()
    fake_lock.release = MagicMock()
    compat._chat_lock = fake_lock

    store = MagicMock()
    store.add_message = AsyncMock(return_value="user-msg-1")

    cm = MagicMock()
    cm.get_context_for_chat = AsyncMock(return_value=[])

    ctx = (
        patch("niu_api.config.get_config", return_value=SimpleNamespace(llm=SimpleNamespace(api_key="test"))),
        patch("agent.session.get_message_store", AsyncMock(return_value=store)),
        patch("agent.context_manager.get_context_manager", AsyncMock(return_value=cm)),
        patch("niu_api.chat.get_or_create_runner", return_value=runner),
        patch("niu_api.chat.notify_new_message", AsyncMock()),
        patch("agent.runner.clear_stop"),
        patch("agent.runner.drain_supplements"),
    )
    return store, ctx


def _run_chat_session(runner):
    """在 mock 环境中运行 compat.chat_session，返回 (resp, notify_calls, persist_calls)。

    运行后恢复 compat._chat_lock 模块全局（防污染其他测试）。
    """
    from niu_api import compat
    from niu_api.compat import chat_session

    old_lock = compat._chat_lock
    store, ctx = _mock_chat_session_env(runner)
    notify_calls = []
    persist_calls = []

    async def fake_persist(*a, **kw):
        persist_calls.append((a, kw))
        return ("persisted-msg-id", "persisted-reply")

    try:
        with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], \
             patch("niu_api.chat.notify_llm_error_sync", side_effect=lambda *a: notify_calls.append(a)), \
             patch("niu_api.chat.persist_agent_reply", side_effect=fake_persist):
            resp = asyncio.run(chat_session(SimpleNamespace(message="hello", source="electron", resources=None)))
    finally:
        compat._chat_lock = old_lock
    return resp, notify_calls, persist_calls


def _llm_error_runner(error_msg="litellm.RateLimitError: You exceeded your current quota", error_type="RateLimitError"):
    """runner：chat 空 chunk 正常返回 + last_return_value 为 LLM_ERROR dict。"""
    runner = MagicMock()
    runner.chat.return_value = iter([])
    rv = {"result": "LLM_ERROR", "error_msg": error_msg}
    if error_type is not None:
        rv["error_type"] = error_type
    runner.last_return_value = rv
    runner.should_push_im.return_value = False
    runner.get_im_channel.return_value = ""
    runner.set_im_channel = MagicMock()
    runner.set_im_force = MagicMock()
    return runner


def test_compat_llm_error_skip_persist_and_notify():
    """LLM_ERROR 分支：skip persist（DB 无错误文本）+ message_id=None 返回正常（无 NameError）+ notify 友好文案。"""
    resp, notify_calls, persist_calls = _run_chat_session(_llm_error_runner())

    assert persist_calls == [], "LLM_ERROR 分支必须 skip persist——错误文本不得落库"
    assert resp.message_id is None  # message_id=None 显式初始化——返回处无条件读取无 NameError
    assert len(notify_calls) == 1
    etype, emsg, src = notify_calls[0]
    assert etype == "RateLimitError"
    assert emsg == _RATE_LIMIT_FRIENDLY  # 真实格式 "litellm.RateLimitError: ..." 命中通道 1
    assert src == "chat_session"


def test_compat_llm_error_no_error_type_text_extraction():
    """LLM_ERROR dict 无 error_type 键（error_type_name 为 None）→ error_type 从原文提取（通道 1 命中）。"""
    resp, notify_calls, persist_calls = _run_chat_session(
        _llm_error_runner(error_msg="litellm.RateLimitError: quota exceeded", error_type=None)
    )

    assert persist_calls == []
    assert resp.message_id is None
    assert len(notify_calls) == 1
    etype, emsg, src = notify_calls[0]
    assert etype == "RateLimitError"  # extract_error_type 从 "litellm.RateLimitError: ..." 提取
    assert emsg == _RATE_LIMIT_FRIENDLY
    assert src == "chat_session"


def test_compat_rv_none_stop_path_no_crash():
    """rv=None（用户 Stop 路径）→ 显式守卫防 None.get AttributeError，正常 persist 不 500。"""
    runner = MagicMock()
    runner.chat.return_value = iter([])
    runner.last_return_value = None  # Stop 路径 rv=None
    runner.should_push_im.return_value = False
    runner.get_im_channel.return_value = ""
    runner.set_im_channel = MagicMock()
    runner.set_im_force = MagicMock()

    resp, notify_calls, persist_calls = _run_chat_session(runner)

    assert len(persist_calls) == 1  # 非 LLM_ERROR → 正常 persist 路径
    assert resp.message_id == "persisted-msg-id"
    assert resp.reply == "persisted-reply"
    assert notify_calls == []


def test_compat_chat_error_litellm_exception_notify_friendly():
    """chat_error 分支：litellm.RateLimitError 实例（chat_error 保留对象后 type() 有效）→ notify 友好文案。"""
    def raise_rate_limit(*a, **kw):
        raise litellm.RateLimitError(message="You exceeded your current quota", llm_provider="openai", model="gpt-4o")

    runner = MagicMock()
    runner.chat.side_effect = raise_rate_limit
    runner.last_return_value = None
    runner.should_push_im.return_value = False
    runner.get_im_channel.return_value = ""
    runner.set_im_channel = MagicMock()
    runner.set_im_force = MagicMock()

    resp, notify_calls, persist_calls = _run_chat_session(runner)

    assert len(notify_calls) == 1
    etype, emsg, src = notify_calls[0]
    assert etype == "RateLimitError"  # type(chat_error).__name__ 有效（保留异常对象）
    assert emsg == _RATE_LIMIT_FRIENDLY
    assert src == "chat_session"
    # full_reply 已是友好文案（Electron Chat 经 llm_error 事件显示）
    assert resp.reply == _RATE_LIMIT_FRIENDLY
    assert resp.message_id is None


def test_compat_non_llm_exception_no_notify():
    """非 LLM 异常（ValueError 内部 bug）→ 不 notify、full_reply 保持既有（不误标"模型调用失败"）。"""
    def raise_value_error(*a, **kw):
        raise ValueError("internal bug")

    runner = MagicMock()
    runner.chat.side_effect = raise_value_error
    runner.last_return_value = None
    runner.should_push_im.return_value = False
    runner.get_im_channel.return_value = ""
    runner.set_im_channel = MagicMock()
    runner.set_im_force = MagicMock()

    resp, notify_calls, persist_calls = _run_chat_session(runner)

    assert notify_calls == []
    assert resp.reply.startswith("Error: ")
    assert "internal bug" in resp.reply


# ===== 源头友好化：agent_loop 两分支（verbose=False 生产路径 + verbose=True） =====

def _run_agent_loop_with_stream_error(verbose, error_type_name, error_msg):
    """以给定 verbose 跑 agent_runner_loop 的 stream_error 分支，返回 (yielded, return_value)。"""
    from agent import runner as _runner_mod
    from agent.generic import agent_loop

    _runner_mod.is_stop_requested = lambda: False
    _runner_mod.clear_stop = lambda: None
    _runner_mod.drain_supplement = lambda: None

    class _FakeValidation:
        is_valid = True
        def format_feedback(self): return ""

    agent_loop.validate_references = lambda content: _FakeValidation()

    class _FakeHandler:
        _last_prompt_tokens = 0
        _done_hooks = []
        max_turns = 1
        current_turn = 1
        def next_prompt_patcher(self, next_prompt, outcome, turn):
            return next_prompt

    def _fake_chat(self, messages, tools=None, response_format=None):
        resp = MockResponse(
            thinking="", content="", tool_calls=[], raw="",
            finish_reason="stop",
            stream_error=True, error_type="retry_exhausted",
            error_msg=error_msg,
            error_type_name=error_type_name,
        )
        yield ""
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
        verbose=verbose,
    )

    yielded = []
    return_value = None
    try:
        while True:
            yielded.append(next(gen))
    except StopIteration as e:
        return_value = e.value
    return yielded, return_value


def test_agent_loop_stream_error_yield_friendly_both_branches():
    """两分支 yield 友好文案（BudgetExceededError 显式类型 → 通道 1）+ LLM_ERROR dict error_type 透传。"""
    for verbose in (False, True):  # verbose=False 是生产路径（runner/subagent 显式传）；verbose=True 测试默认值可达
        yielded, rv = _run_agent_loop_with_stream_error(
            verbose=verbose,
            error_type_name="BudgetExceededError",
            error_msg="Budget has been exceeded!",
        )
        texts = [y for y in yielded if isinstance(y, str)]
        assert any(_QUOTA_FRIENDLY in t for t in texts), f"verbose={verbose} yield 非友好文案: {texts!r}"
        assert rv.get("result") == "LLM_ERROR"
        assert rv.get("error_type") == "BudgetExceededError", f"verbose={verbose} dict 未透传 error_type"


def test_agent_loop_stream_error_no_type_name_channel3():
    """无 error_type_name（通道 3 保底）→ yield 裸原文/保底，dict error_type=None，不抛异常。"""
    yielded, rv = _run_agent_loop_with_stream_error(
        verbose=False,
        error_type_name=None,
        error_msg="raw connection failure",
    )
    texts = [y for y in yielded if isinstance(y, str)]
    assert any("raw connection failure" in t for t in texts), f"yield 应含裸原文: {texts!r}"
    assert rv.get("result") == "LLM_ERROR"
    assert rv.get("error_type") is None


# ===== 透传链 adapter 侧：覆盖点同步 + happy path =====

def _make_chunk(content=None, finish_reason=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)], usage=None)


def _exhaust_chat(session):
    """跑完 session.chat 生成器，返回最终 MockResponse。"""
    gen = session.chat(messages=[{"role": "user", "content": "test"}], tools=None)
    result = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        result = e.value
    return result


def _session():
    from agent.generic.litellm_adapter import LiteLLMSession
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    return LiteLLMSession(cfg)


def test_retry_exhausted_error_type_name_synced():
    """重试耗尽 → error_type_name 同步为末错类型（对应末错文本）；error_type 内部类别语义不动。"""
    session = _session()

    def mock_completion(**kwargs):
        def gen():
            yield _make_chunk(content="partial")
            raise litellm.APIConnectionError(message="burst protection", model="test", llm_provider="test")
        return gen()

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        result = _exhaust_chat(session)

    assert result.stream_error is True
    assert result.error_type == "retry_exhausted"  # 内部类别语义不动（llm_proxy 消费）
    assert result.error_type_name == "APIConnectionError"  # 末错类型
    assert "burst protection" in result.error_msg  # 末错文本


def test_socket_fallback_failure_error_type_name_synced():
    """socket fallback 失败 → error_type_name 同步为 fallback 末错类型（对应 fallback 末错文本）。"""
    session = _session()

    def mock_completion(**kwargs):
        if kwargs.get("stream") is False:
            raise litellm.Timeout(message="fallback timeout", model="test", llm_provider="test")
        def gen():
            yield _make_chunk(content="partial")
            raise litellm.APIConnectionError(message="10054 socket reset", model="test", llm_provider="test")
        return gen()

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        result = _exhaust_chat(session)

    assert result.stream_error is True
    assert result.error_type == "retry_exhausted"
    assert result.error_type_name == "Timeout"  # 末错类型 = fallback 错
    assert "fallback timeout" in result.error_msg  # 末错文本 = fallback 错


def test_stream_error_type_name_budget_exceeded_channel1():
    """流中段 BudgetExceededError（str() 无类名）→ error_type_name 记录 → 显式类型通道 1 翻译。"""
    session = _session()

    class FakeBudgetError(Exception):
        pass

    def mock_completion(**kwargs):
        def gen():
            yield _make_chunk(content="partial")
            raise FakeBudgetError("Budget has been exceeded!")
        return gen()

    with patch("litellm.completion", side_effect=mock_completion), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        result = _exhaust_chat(session)

    assert result.stream_error is True
    assert result.error_type_name == "FakeBudgetError"
    # agent_loop 侧拿到 error_type_name 后 format 出通道 1 中文——全链路一致由 agent_loop 测试锁定
    from agent.generic.litellm_adapter import format_llm_error_for_user
    assert format_llm_error_for_user(result.error_msg, result.error_type_name) == "模型调用失败（FakeBudgetError）：Budget has been exceeded!"


def test_happy_path_mock_response_construction_no_nameerror():
    """happy path 成功调用最终 MockResponse 构造无 NameError（_stream_error_type_name 初始化）。"""
    session = _session()
    good_chunks = [_make_chunk(content="hello"), _make_chunk(finish_reason="stop")]

    with patch("litellm.completion", return_value=iter(good_chunks)), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        result = _exhaust_chat(session)

    assert result is not None
    assert result.stream_error is False
    assert result.error_type_name is None  # 初始化值
    assert result.content == "hello"


# ===== import 无循环依赖 =====

def test_imports_no_circular_dependency():
    """E2 Task 2 涉及模块可独立导入（无循环依赖）——与 test_llm_error_formatting 共存不崩。"""
    import niu_api.chat  # noqa: F401
    import niu_api.compat  # noqa: F401
    import agent.generic.agent_loop  # noqa: F401
    import agent.generic.litellm_adapter  # noqa: F401
    import agent.generic.llmcore  # noqa: F401
