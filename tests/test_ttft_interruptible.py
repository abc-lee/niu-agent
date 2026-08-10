# tests/test_ttft_interruptible.py
"""LLM 调用建立（TTFT）可中断测试。

覆盖：litellm.completion() 慢速（TTFT 挂起）+ stop → 放弃返回空 MockResponse（不阻塞等 read_timeout）；
正常路径 completion 返回流式照常。
全 mock litellm.completion，不调真实 LLM。
"""
import threading
import time

import pytest

from agent.generic import litellm_adapter as la


def _make_session(stop_check):
    """构造最小 LiteLLMSession（绕过真实配置）。"""
    session = la.LiteLLMSession.__new__(la.LiteLLMSession)
    session.stop_check = stop_check
    session.api_base = "http://localhost:9999/v1"
    session.api_key = "test"
    session.default_model = "test-model"
    session.api_type = "openai"
    session.provider = ""
    session.litellm_kwargs = {}
    session.proxies = None
    session.temperature = None
    session.read_timeout = 300
    session.reasoning_effort = None
    return session


def test_ttft_abandons_on_stop(monkeypatch):
    """completion() 慢速（TTFT 挂起）+ stop 置位：放弃等待，快速返回空 MockResponse。"""
    stop_flag = {"v": False}

    def _slow_completion(**kwargs):
        time.sleep(0.5)  # 模拟 TTFT 挂起（真实场景挂到 read_timeout）
        raise AssertionError("completion should not complete when abandoned")

    monkeypatch.setattr(la.litellm, "completion", _slow_completion)
    monkeypatch.setattr(la, "_write_interaction_log", lambda *a, **kw: None)
    monkeypatch.setattr(la, "_write_raw_log", lambda *a, **kw: None)
    threading.Timer(0.05, lambda: stop_flag.__setitem__("v", True)).start()

    session = _make_session(lambda: stop_flag["v"])
    started = time.monotonic()
    gen = session.chat(messages=[{"role": "user", "content": "hi"}])
    # 消费生成器取 StopIteration.value（MockResponse）
    try:
        while True:
            next(gen)
    except StopIteration as e:
        resp = e.value
    elapsed = time.monotonic() - started
    assert elapsed < 0.4  # 放弃等待（预检/0.2s 轮询 + 余量）
    assert resp is not None
    assert resp.content == ""
    assert resp.tool_calls == []


def test_ttft_normal_completes(monkeypatch):
    """无停止：completion() 返回流式照常消费。"""
    class _FakeStream:
        """最小流式：一个 chunk 后 done。"""
        def __init__(self):
            self._done = False

        def __iter__(self):
            return self

        def __next__(self):
            if self._done:
                raise StopIteration
            self._done = True
            chunk = type("C", (), {
                "choices": [type("Ch", (), {"delta": type("D", (), {"content": "hi"}), "finish_reason": "stop"})],
                "usage": None,
            })()
            return chunk

    monkeypatch.setattr(la.litellm, "completion", lambda **kw: _FakeStream())
    monkeypatch.setattr(la, "_write_interaction_log", lambda *a, **kw: None)
    monkeypatch.setattr(la, "_write_raw_log", lambda *a, **kw: None)
    session = _make_session(lambda: False)
    gen = session.chat(messages=[{"role": "user", "content": "hi"}])
    chunks = []
    while True:
        try:
            chunks.append(next(gen))
        except StopIteration as e:
            resp = e.value
            break
    assert resp.content == "hi"


def test_retry_ttft_abandons_on_stop(monkeypatch):
    """R6-B P2-3 补充：重试路径 completion() 慢速 + stop → 放弃，返回已积累内容 MockResponse。

    R7 P0-1 修正：首轮错误必须发生在**流式消费阶段**（初始 completion 的异常走 init_err
    分支直接 re-raise，永远进不了重试循环）——第一轮返回迭代时抛 TimeoutError 的流
    （_do_streaming_completion → _classify_stream_error(TimeoutError)='retryable' → 重试）。

    R8-B P1 修正（确定性 stop 注入，消除墙钟竞态）：不能用测试顶部 Timer 置位 stop——
    重试循环入口 L805 `if self.stop_check(): break` 是循环体第一条语句，stop 先置位则
    重试从未发起（call_count==1）且 stream_error=True，双断言失败。stop 置位挪进
    _completion 的重试分支内部（call_count==2 时才执行）：重试 _ri 后台线程启动时
    stop 必为 False（预检 + L805 均通过）→ 前台 poll1 Empty@~205ms（stop 未置）→
    poll2 Empty@~405ms → stop True（0.25s 后置位）→ 放弃。**stop 置位后必须再睡满
    ≥1 个 0.2s 轮询周期再 raise**——否则 error 先于放弃入队被前台 raise → except 接住
    → 下一轮 L805 break → stream_error 仍 True，测试仍败。
    """
    stop_flag = {"v": False}
    call_count = {"n": 0}

    class _ErrorThenSlowStream:
        """第一轮：迭代时抛 TimeoutError（触发重试）；后续轮：慢速挂起。"""

        def __init__(self):
            self._called = False

        def __iter__(self):
            return self

        def __next__(self):
            if not self._called:
                self._called = True
                raise TimeoutError("simulated stream error")
            raise StopIteration

    def _completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _ErrorThenSlowStream()
        # 重试分支（仅 call_count==2 到达，即重试 _ri 后台线程内）——确定性 stop 注入：
        time.sleep(0.25)          # 重试 TTFT 挂起（前台首个 0.2s 轮询 Empty，stop 未置）
        stop_flag["v"] = True     # 必在重试 _ri 预检与 L805 检查之后置位
        time.sleep(0.25)          # 保持挂起满 ≥1 轮询周期（防 error 先于放弃入队）
        raise AssertionError("retry completion should not complete when abandoned")

    monkeypatch.setattr(la.litellm, "completion", _completion)
    monkeypatch.setattr(la, "_write_interaction_log", lambda *a, **kw: None)
    monkeypatch.setattr(la, "_write_raw_log", lambda *a, **kw: None)

    session = _make_session(lambda: stop_flag["v"])
    started = time.monotonic()
    gen = session.chat(messages=[{"role": "user", "content": "hi"}])
    try:
        while True:
            next(gen)
    except StopIteration as e:
        resp = e.value
    elapsed = time.monotonic() - started
    assert elapsed < 0.5  # 重试放弃（2 轮询周期 ~0.41s + 余量）
    assert call_count["n"] == 2  # 首轮错误 + 重试（被放弃）
    assert resp is not None
    assert resp.stream_error is False  # 放弃非错误 → 不判 LLM_ERROR → L1058 STOPPED
