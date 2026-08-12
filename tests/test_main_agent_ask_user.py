"""主 Agent ask_user 工具测试（暂停问话，工作流不中断）。"""
import asyncio  # R4-A P1：fixture seed _event_subscribers 用 asyncio.Queue()——缺 import 则 NameError
import threading
import pytest

from agent.ask_user import get_user_ask_registry


@pytest.fixture
def fake_loop(monkeypatch):
    loop = _FakeMainLoop()
    monkeypatch.setattr("niu_api.chat._main_loop", loop)
    # R3-P1-1：do_ask_user 推送闸门要求 _event_subscribers 非空（R2-P1-4）——
    # 测试环境无 SSE 连接，必须 seed（否则全部走"无法显示"错误分支）
    monkeypatch.setattr("niu_api.chat._event_subscribers", [asyncio.Queue()])
    return loop


class _FakeMainLoop:
    """_sync_broadcast(event) 是必选 1 参——call_soon_threadsafe(fn, event) 必须 fn(event)。"""

    def __init__(self):
        self.events = []
        self._closed = False

    def is_closed(self):
        return self._closed

    def call_soon_threadsafe(self, fn, event):
        self.events.append(event)
        fn(event)  # 传 event（R2-B P1-2：旧版 fn() 无参 → 真实 _sync_broadcast TypeError 逃逸）


@pytest.fixture
def handler():
    from agent.handler import NiuHandler
    h = NiuHandler.__new__(NiuHandler)  # 不执行 __init__（避免依赖）
    return h


def _set_answer_after(registry, delay=0.05, answer="42"):
    """后台线程稍后回答，模拟用户输入。"""
    def _do():
        import time
        time.sleep(delay)
        registry.set_answer("main-agent", answer)
    t = threading.Thread(target=_do, daemon=True)
    t.start()
    return t


def test_ask_user_schema_in_tools(monkeypatch):
    """get_tools_schema(include_main_only=True) 含 ask_user；False 不含（子 Agent 不可见）。"""
    monkeypatch.setattr("agent.subagent._USER_AGENTS_DIR", "/tmp/nonexistent-agents-dir")
    from agent.runner import get_tools_schema
    schema = get_tools_schema(include_main_only=True)
    names = [t["function"]["name"] for t in schema]
    assert "ask_user" in names
    sub_schema = get_tools_schema(include_main_only=False)
    sub_names = [t["function"]["name"] for t in sub_schema]
    assert "ask_user" not in sub_names


def test_ask_user_returns_answer(fake_loop, handler, monkeypatch):
    """do_ask_user 推 SSE + 等待回答 → 返回 [user 回答]。"""
    # do_ask_user 函数体内 `from agent.ask_user import ... get_user_ask_registry`——
    # 必须 patch agent.ask_user.get_user_ask_registry（patch agent.handler 同名属性无效，会 AttributeError）
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    _set_answer_after(registry)
    out = handler.do_ask_user({"question": "继续吗？"}, response=None)
    assert out.data == "[user 回答] 42"
    assert fake_loop.events and fake_loop.events[0]["type"] == "ask_user"
    assert fake_loop.events[0]["content"] == "继续吗？"
    # finally unregister（防 is_waiting 脏）
    assert not registry.is_waiting("main-agent")


def test_ask_user_timeout(fake_loop, handler, monkeypatch):
    """超时（future.wait 返回 None）→ 返回 [ask_user 超时] 提示。"""
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    monkeypatch.setattr("agent.ask_user._ASK_TIMEOUT", 0.01)
    out = handler.do_ask_user({"question": "还在吗？"}, response=None)
    assert "[ask_user 超时]" in out.data
    assert not registry.is_waiting("main-agent")  # finally unregister


def test_ask_user_terminated(fake_loop, handler, monkeypatch):
    """终止（TERMINATED_SIGNAL）→ 返回 [ask_user 已终止]。"""
    from agent.ask_user import TERMINATED_SIGNAL
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)

    def _terminate():
        import time
        time.sleep(0.05)
        registry.set_answer("main-agent", TERMINATED_SIGNAL)
    threading.Thread(target=_terminate, daemon=True).start()
    out = handler.do_ask_user({"question": "要停止吗？"}, response=None)
    assert "[ask_user 已终止]" in out.data
    assert not registry.is_waiting("main-agent")  # finally unregister


def test_ask_user_unavailable(fake_loop, handler, monkeypatch):
    """前端无可渲染窗口（main.js 回执 UNAVAILABLE_SIGNAL）→ 返回 [ask_user 无法显示]。"""
    from agent.ask_user import UNAVAILABLE_SIGNAL
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)

    def _unavailable():
        import time
        time.sleep(0.05)
        registry.set_answer("main-agent", UNAVAILABLE_SIGNAL)
    threading.Thread(target=_unavailable, daemon=True).start()
    out = handler.do_ask_user({"question": "窗口关了吗？"}, response=None)
    assert "[ask_user 无法显示]" in out.data
    assert not registry.is_waiting("main-agent")


def test_ask_user_stop_early_return(handler, monkeypatch):
    """R8-A/B P3：/stop 已置位（is_stop_requested True）→ 推送前/register 后早退返回已终止。"""
    from agent.ask_user import get_user_ask_registry
    from agent.runner import clear_stop, request_stop
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    try:
        request_stop()  # 置位全局停止标志（推送前检查捕获）
        out = handler.do_ask_user({"question": "停了吗？"}, response=None)
        assert "[ask_user 已终止]" in out.data
        assert not registry.is_waiting("main-agent")
    finally:
        clear_stop()  # 必须清除——泄漏会让本文件后续测试全部早退失败


class _FakeGateway:
    def __init__(self, connected=True, raise_on_send=False):
        self.streamed = []
        self.sent = []  # (channel_id, content, pop_reply_to, ask_finalize)
        self.is_connected = connected
        self._raise_on_send = raise_on_send

    def notify_stream(self, content, channel_id="", is_final=False):
        self.streamed.append((content, channel_id, is_final))

    def send_sync(self, channel_id, content, pop_reply_to=False, ask_finalize=False):
        if self._raise_on_send:
            raise RuntimeError("send_sync boom")
        self.sent.append((channel_id, content, pop_reply_to, ask_finalize))


class _FakeRunnerForIM:
    _current_channel_id = "im:123"


def test_ask_user_im_push(fake_loop, handler, monkeypatch):
    """R1：Electron SSE 无订阅者时走 IM 通道推送（send_sync 终结+问题）——推送成功即继续等待。"""
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    monkeypatch.setattr("niu_api.chat._event_subscribers", [])  # SSE 无订阅者 → electron_pushed=False
    gw = _FakeGateway()
    # do_ask_user 函数体内 `from agent.runner import get_runner` / `from niu_api.channel.gateway import get_im_gateway`
    # ——patch 源模块
    monkeypatch.setattr("agent.runner.get_runner", lambda: _FakeRunnerForIM())
    monkeypatch.setattr("niu_api.channel.gateway.get_im_gateway", lambda: gw)
    _set_answer_after(registry)
    out = handler.do_ask_user({"question": "飞书继续吗？"}, response=None)
    assert out.data == "[user 回答] 42"
    # 终结空消息 + 问题独立消息（pop_reply_to=False, ask_finalize=True）
    assert [c for _, c, _, _ in gw.sent] == ["", "❓ 飞书继续吗？"]
    assert all(pop is False and fin is True for _, _, pop, fin in gw.sent)
    assert not registry.is_waiting("main-agent")


def test_ask_user_dual_channel_both_pushed(fake_loop, handler, monkeypatch):
    """双端场景（Electron SSE 订阅者非空 + IM 通道存在）：双通道独立推送——去掉 if not pushed 门控，
    Electron 推成功不再跳过 IM，飞书也收到问题。"""
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    # fake_loop 已 seed _event_subscribers 非空 → Electron 推成功（electron_pushed=True）
    gw = _FakeGateway()
    monkeypatch.setattr("agent.runner.get_runner", lambda: _FakeRunnerForIM())
    monkeypatch.setattr("niu_api.channel.gateway.get_im_gateway", lambda: gw)
    _set_answer_after(registry)
    out = handler.do_ask_user({"question": "双端同步？"}, response=None)
    assert out.data == "[user 回答] 42"
    # Electron SSE 事件已推
    assert fake_loop.events and fake_loop.events[0]["type"] == "ask_user"
    assert fake_loop.events[0]["content"] == "双端同步？"
    # IM 也推了（终结 + 问题）——不再被 electron_pushed 门控跳过
    assert [c for _, c, _, _ in gw.sent] == ["", "❓ 双端同步？"]
    assert not registry.is_waiting("main-agent")


def test_ask_user_im_exception_keeps_electron(fake_loop, handler, monkeypatch):
    """IM send_sync 异常只置 im_pushed=False，不拖累已成功的 Electron 推送（继续等待回答）。"""
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    gw = _FakeGateway(raise_on_send=True)  # IM 推送抛异常 → im_pushed=False
    monkeypatch.setattr("agent.runner.get_runner", lambda: _FakeRunnerForIM())
    monkeypatch.setattr("niu_api.channel.gateway.get_im_gateway", lambda: gw)
    _set_answer_after(registry)
    out = handler.do_ask_user({"question": "IM 挂了？"}, response=None)
    assert out.data == "[user 回答] 42"  # Electron 通道仍成立，不走无法显示
    assert fake_loop.events and fake_loop.events[0]["content"] == "IM 挂了？"
    assert not registry.is_waiting("main-agent")


def test_ask_user_im_unavailable_when_no_channel(handler, monkeypatch):
    """R1：SSE 无订阅者 + 无 IM 通道（current_channel_id 空）→ 无法显示错误分支（不阻塞）。"""
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    monkeypatch.setattr("niu_api.chat._event_subscribers", [])
    monkeypatch.setattr("niu_api.chat._main_loop", None)
    runner = _FakeRunnerForIM()
    runner._current_channel_id = ""  # 无 IM 通道
    monkeypatch.setattr("agent.runner.get_runner", lambda: runner)
    out = handler.do_ask_user({"question": "无通道？"}, response=None)
    assert "[ask_user 无法显示]" in out.data
    assert not registry.is_waiting("main-agent")


def test_ask_user_subagent_guard(handler):
    """P2-1：子 Agent（_is_subagent=True）调用 ask_user 直接返回错误，不劫持 main-agent future。"""
    handler._is_subagent = True
    out = handler.do_ask_user({"question": "劫持？"}, response=None)
    assert out.data["status"] == "error"
    assert "仅主 Agent" in out.data["msg"]
    assert not get_user_ask_registry().is_waiting("main-agent")


def test_ask_user_stop_between_precheck_and_register(fake_loop, handler, monkeypatch):
    """P3-7：/stop 落在"推送前预检→register 后复查"毫秒窗口——复查捕获，返回已终止且无残留。

    副作用序列：第一次 is_stop_requested() 返回 False（推送通过），第二次返回 True（register 后复查）。
    """
    import agent.runner as runner_mod
    registry = get_user_ask_registry()
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    calls = {"n": 0}

    def _is_stop_requested():
        calls["n"] += 1
        return calls["n"] >= 2  # 推送前 False → register 后 True

    monkeypatch.setattr(runner_mod, "is_stop_requested", _is_stop_requested)
    out = handler.do_ask_user({"question": "复查窗口？"}, response=None)
    assert calls["n"] == 2  # 两个检查点都走到
    assert "[ask_user 已终止]" in out.data
    assert not registry.is_waiting("main-agent")


def test_ask_answer_endpoint_ok():
    """P3-7：ask-answer 端点——有 pending ask 时注入成功且解除等待。"""
    import asyncio
    from niu_api.chat import AskAnswerRequest, ask_answer
    registry = get_user_ask_registry()
    registry.unregister("main-agent")  # 确保干净起点
    registry.register("main-agent")
    try:
        res = asyncio.run(ask_answer(AskAnswerRequest(answer="42")))
        assert res == {"ok": True}
        assert not registry.is_waiting("main-agent")
    finally:
        registry.unregister("main-agent")


def test_ask_answer_endpoint_no_pending():
    """P3-7：ask-answer 端点——无 pending ask 返回 no pending ask（窗口关闭回执等场景无害）。"""
    import asyncio
    from niu_api.chat import AskAnswerRequest, ask_answer
    registry = get_user_ask_registry()
    registry.unregister("main-agent")  # 确保干净起点
    res = asyncio.run(ask_answer(AskAnswerRequest(answer="42")))
    assert res == {"ok": False, "error": "no pending ask"}


def test_ask_answer_endpoint_empty_answer_rejected():
    """P3-7：ask-answer 端点——空白回答被 pydantic 拒绝（min_length=1 + strip，空回答 400 契约）。"""
    import pytest
    from pydantic import ValidationError
    from niu_api.chat import AskAnswerRequest
    with pytest.raises(ValidationError):
        AskAnswerRequest(answer="   ")
    with pytest.raises(ValidationError):
        AskAnswerRequest(answer="")
