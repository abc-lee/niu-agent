"""子 Agent ask_user（_ask_user_impl）IM 推送测试。

覆盖：IM 会话（_current_channel_id 有值）→ send_sync 终结当前流式卡片 + 问题独立消息
（pop_reply_to=False, ask_finalize=True，与主 Agent do_ask_user 对齐）；
Electron 会话（_cid 空）→ 跳过 IM 推送不影响等待；send_sync 异常 → 优雅降级。
"""
from agent.ask_user import AskUserFuture


class _FakeRegistry:
    """register 返回已完成的 future（避免真实 600s 阻塞 / 线程竞态）。"""

    def __init__(self, answer="42"):
        self.f = AskUserFuture()
        self.f.set_answer(answer)
        self.registered = []
        self.unregistered = []

    def register(self, unique_name):
        self.registered.append(unique_name)
        return self.f

    def unregister(self, unique_name):
        self.unregistered.append(unique_name)


class _FakeRunner:
    def __init__(self, channel_id="ch1"):
        self._current_channel_id = channel_id


class _FakeGateway:
    def __init__(self, connected=True, raise_on_send=False):
        self.is_connected = connected
        self.raise_on_send = raise_on_send
        self.calls = []

    def send_sync(self, channel_id, content, pop_reply_to=True, ask_finalize=False):
        if self.raise_on_send:
            raise RuntimeError("send_sync boom")
        self.calls.append((channel_id, content, pop_reply_to, ask_finalize))


class _FakeSubagent:
    def __init__(self):
        self.state = "running"
        self._ask_user_terminated = False


def _patch_ask_user_env(monkeypatch, runner, gateway, subagent):
    """统一 patch _ask_user_impl 函数体依赖（函数体内 import，patch 模块属性生效）。"""
    monkeypatch.setattr(
        "agent.subagent_registry.SubagentRegistry.get",
        staticmethod(lambda name: subagent),
    )
    monkeypatch.setattr(
        "agent.ask_user.get_user_ask_registry",
        lambda: _FakeRegistry(),
    )
    monkeypatch.setattr(
        "niu_api.internal.subagent_event_bus.notify_subagent_event_sync",
        lambda unique_name, event_type, payload: None,
    )
    monkeypatch.setattr("agent.runner.get_runner", lambda: runner)
    monkeypatch.setattr("niu_api.channel.gateway.get_im_gateway", lambda: gateway)


def test_ask_user_impl_im_push_finalize_and_question(monkeypatch):
    """IM 会话：先 send_sync("") 终结当前流式卡片，再 send_sync("问题") 独立消息。"""
    from agent.subagent import _ask_user_impl

    runner = _FakeRunner("ch1")
    gw = _FakeGateway()
    sub = _FakeSubagent()
    _patch_ask_user_env(monkeypatch, runner, gw, sub)

    answer = _ask_user_impl("你的问题?", "sub-1")

    assert answer == "42"
    assert gw.calls == [
        ("ch1", "", False, True),            # 终结当前流式卡片（adapter 用 accumulated 定稿）
        ("ch1", "你的问题?", False, True),  # 问题作独立消息即时显示
    ]
    assert sub.state == "running"  # 等待结束恢复 state


def test_ask_user_impl_skips_im_when_no_channel(monkeypatch):
    """Electron 会话（_current_channel_id 空）：不调 send_sync，等待回答不受影响。"""
    from agent.subagent import _ask_user_impl

    runner = _FakeRunner("")  # Electron：无 IM 通道
    gw = _FakeGateway()
    _patch_ask_user_env(monkeypatch, runner, gw, _FakeSubagent())

    answer = _ask_user_impl("问题?", "sub-2")

    assert answer == "42"
    assert gw.calls == []


def test_ask_user_impl_im_push_exception_swallowed(monkeypatch):
    """send_sync 异常被 try/except 吞掉——问题照常等待回答，不破坏原路径。"""
    from agent.subagent import _ask_user_impl

    runner = _FakeRunner("ch1")
    gw = _FakeGateway(raise_on_send=True)
    _patch_ask_user_env(monkeypatch, runner, gw, _FakeSubagent())

    answer = _ask_user_impl("问题?", "sub-3")

    assert answer == "42"


def test_ask_user_impl_im_push_not_connected_skips(monkeypatch):
    """网关未连接（is_connected=False）：跳过推送，等待回答不受影响。"""
    from agent.subagent import _ask_user_impl

    runner = _FakeRunner("ch1")
    gw = _FakeGateway(connected=False)
    _patch_ask_user_env(monkeypatch, runner, gw, _FakeSubagent())

    answer = _ask_user_impl("问题?", "sub-4")

    assert answer == "42"
    assert gw.calls == []


def test_ask_user_impl_im_push_exception_logs_error(monkeypatch):
    """E4-04：send_sync 异常被吞掉（不中断子 Agent——AskUserFuture 超时兜底），
    但记录 logger.error（含异常文本）——不再静默 pass。

    吞异常语义保持 + 日志断言：error 日志含异常文本，回答照常返回。
    """
    from loguru import logger

    from agent.subagent import _ask_user_impl

    runner = _FakeRunner("ch1")
    gw = _FakeGateway(raise_on_send=True)
    _patch_ask_user_env(monkeypatch, runner, gw, _FakeSubagent())

    messages = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="ERROR")
    try:
        answer = _ask_user_impl("问题?", "sub-3")
    finally:
        logger.remove(sink_id)

    assert answer == "42"  # 吞异常语义保持——问题照常等待回答
    assert any("send_sync boom" in m for m in messages), (
        f"应记录 error 含异常文本，实际: {messages}"
    )
