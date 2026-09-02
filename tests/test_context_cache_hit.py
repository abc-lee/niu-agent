"""上下文缓存命中率显示链路测试。

覆盖：
1. 适配层 _extract_cached_tokens：对象/dict 两种 prompt_tokens_details 形态、缺失/非法值降级
2. 流式 chat 全链路：末 chunk usage 带/不带 cached 细节 → MockResponse.usage["cached_tokens"]
   与 mock_resp.cached_tokens 两态
3. agent_loop 消费点：usage.cached_tokens → handler._last_cached_tokens
4. get_stats：StatsResponse.context_cache_hit 字段（有命中→比率；未知(None)→None；cached=0→0.0 真实零命中）
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.generic.llmcore import MockResponse
from agent.generic.litellm_adapter import LiteLLMSession, _extract_cached_tokens


# ---------------------------------------------------------------------------
# 1. 提取函数
# ---------------------------------------------------------------------------

def test_extract_from_object_details():
    """litellm 归一化形态：prompt_tokens_details 为对象。"""
    usage = SimpleNamespace(
        prompt_tokens=10000,
        completion_tokens=50,
        total_tokens=10050,
        prompt_tokens_details=SimpleNamespace(cached_tokens=8700),
    )
    assert _extract_cached_tokens(usage) == 8700


def test_extract_from_dict_details():
    """details 是 dict 的防御分支。"""
    usage = SimpleNamespace(prompt_tokens_details={"cached_tokens": 123})
    assert _extract_cached_tokens(usage) == 123


def test_extract_missing_details_returns_none():
    """服务端未返回缓存细节 → None（区分"未知"与 0）。"""
    usage = SimpleNamespace(prompt_tokens=100, prompt_tokens_details=None)
    assert _extract_cached_tokens(usage) is None
    usage2 = SimpleNamespace(prompt_tokens=100)
    assert _extract_cached_tokens(usage2) is None


def test_extract_zero_and_invalid():
    """cached=0 是合法真值返回 0；非数值降级 None。"""
    usage = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=0))
    assert _extract_cached_tokens(usage) == 0
    usage_bad = SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens="many"))
    assert _extract_cached_tokens(usage_bad) is None


# ---------------------------------------------------------------------------
# 2. 流式 chat 全链路（参照 test_llm_error_handling 的 chunk 构造模式）
# ---------------------------------------------------------------------------

def _make_chunk(content=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=usage,
    )


def _exhaust_chat(session, chunks):
    with patch("litellm.completion", return_value=iter(chunks)), \
         patch("agent.generic.litellm_adapter.is_stop_requested", return_value=False):
        gen = session.chat(messages=[{"role": "user", "content": "test"}], tools=None)
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value
    return result


def _session():
    cfg = {"apikey": "test", "apibase": "http://test", "model": "test-model", "read_timeout": 30}
    return LiteLLMSession(cfg)


def test_stream_chat_preserves_cached_tokens():
    """流式末 chunk usage 带缓存细节 → usage dict 与属性都保留。"""
    usage = SimpleNamespace(
        prompt_tokens=31072,
        completion_tokens=200,
        total_tokens=31272,
        prompt_tokens_details=SimpleNamespace(cached_tokens=28000),
    )
    chunks = [_make_chunk(content="hi"), _make_chunk(finish_reason="stop", usage=usage)]
    resp = _exhaust_chat(_session(), chunks)
    assert isinstance(resp, MockResponse)
    assert resp.cached_tokens == 28000
    assert resp.usage["cached_tokens"] == 28000
    assert resp.usage["prompt_tokens"] == 31072


def test_stream_chat_without_cache_details_degrades_to_none():
    """服务端不返回缓存细节（如 doubao 未返）→ None，不伪装 0。"""
    usage = SimpleNamespace(
        prompt_tokens=5000,
        completion_tokens=10,
        total_tokens=5010,
        prompt_tokens_details=None,
    )
    chunks = [_make_chunk(content="hi"), _make_chunk(finish_reason="stop", usage=usage)]
    resp = _exhaust_chat(_session(), chunks)
    assert resp.cached_tokens is None
    assert resp.usage["cached_tokens"] is None


# ---------------------------------------------------------------------------
# 3. agent_loop 消费点
# ---------------------------------------------------------------------------

def exhaust(gen):
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


class FakeClient:
    def __init__(self, response):
        self.name = "mock"
        self.last_tools = ""
        self.total_cd_tokens = 0
        self._response = response

    def chat(self, messages, tools=None):
        resp = self._response

        def gen():
            yield resp
            return resp

        return gen()


class FakeHandler:
    def __init__(self):
        self._done_hooks = []
        self.max_turns = 40
        self.current_turn = 0
        self._current_messages = []
        self._last_prompt_tokens = 0
        self._last_cached_tokens = 0

    def dispatch(self, tool_name, args, response, index=0):
        from agent.generic.agent_loop import StepOutcome

        def gen():
            yield ""
            return StepOutcome(None, next_prompt="continue", should_exit=False)

        return gen()

    def next_prompt_patcher(self, next_prompt, outcome, turn):
        return next_prompt

    def tool_before_callback(self, tool_name, args, response):
        pass

    def tool_after_callback(self, tool_name, args, response, ret):
        pass


def _run_loop(response):
    from agent.generic.agent_loop import agent_runner_loop
    handler = FakeHandler()
    client = FakeClient(response)
    exhaust(agent_runner_loop(
        client=client,
        system_prompt="system",
        user_input="hello",
        handler=handler,
        tools_schema=[],
        max_turns=5,
        verbose=False,
        context_window_tokens=0,
        context_fifo_threshold=0,
        enable_supplement=False,
    ))
    return handler


def test_agent_loop_captures_last_cached_tokens(monkeypatch):
    """usage dict 含 cached_tokens → handler._last_cached_tokens 捕获。"""
    monkeypatch.setattr("agent.runner.is_stop_requested", lambda: False)
    monkeypatch.setattr("agent.runner.clear_stop", lambda: None)
    monkeypatch.setattr("agent.runner.drain_supplement", lambda: None)
    resp = MockResponse(thinking="", content="Done", tool_calls=[], raw="Done",
                        usage={"prompt_tokens": 20000, "completion_tokens": 10,
                               "total_tokens": 20010, "cached_tokens": 18000})
    handler = _run_loop(resp)
    assert handler._last_prompt_tokens == 20000
    assert handler._last_cached_tokens == 18000


def test_agent_loop_missing_cached_stays_none(monkeypatch):
    """usage 无 cached_tokens 键 → 置 None（未知），不抛异常。"""
    monkeypatch.setattr("agent.runner.is_stop_requested", lambda: False)
    monkeypatch.setattr("agent.runner.clear_stop", lambda: None)
    monkeypatch.setattr("agent.runner.drain_supplement", lambda: None)
    resp = MockResponse(thinking="", content="Done", tool_calls=[], raw="Done",
                        usage={"prompt_tokens": 3000, "completion_tokens": 5, "total_tokens": 3005})
    handler = _run_loop(resp)
    assert handler._last_prompt_tokens == 3000
    assert handler._last_cached_tokens is None


def test_agent_loop_zero_cached_is_real_zero(monkeypatch):
    """usage.cached_tokens=0 是真实零命中 → handler 保留 0 而非 None。"""
    monkeypatch.setattr("agent.runner.is_stop_requested", lambda: False)
    monkeypatch.setattr("agent.runner.clear_stop", lambda: None)
    monkeypatch.setattr("agent.runner.drain_supplement", lambda: None)
    resp = MockResponse(thinking="", content="Done", tool_calls=[], raw="Done",
                        usage={"prompt_tokens": 3000, "completion_tokens": 5, "total_tokens": 3005,
                               "cached_tokens": 0})
    handler = _run_loop(resp)
    assert handler._last_prompt_tokens == 3000
    assert handler._last_cached_tokens == 0


# ---------------------------------------------------------------------------
# 4. get_stats 字段
# ---------------------------------------------------------------------------

class _FakeStore:
    async def count_messages(self):
        return 1



def _patch_stats_env(monkeypatch, real_tokens, cached_tokens, window=100000):
    import niu_api.compat as compat
    import niu_api.chat as chat_mod

    class _Runner:
        def __init__(self):
            self.handler = SimpleNamespace(_last_prompt_tokens=real_tokens,
                                           _last_cached_tokens=cached_tokens)

    async def fake_get_message_store():
        return _FakeStore()

    monkeypatch.setattr(compat, "get_message_store", fake_get_message_store)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: window)
    monkeypatch.setattr(chat_mod, "get_or_create_runner", lambda: _Runner())


def test_get_stats_reports_hit_ratio(monkeypatch):
    """有真实 cached → 命中率 = cached/prompt。"""
    from niu_api.compat import get_stats
    _patch_stats_env(monkeypatch, real_tokens=20000, cached_tokens=18000)
    stats = asyncio.run(get_stats())
    assert stats.context_usage == 0.2
    assert abs(stats.context_cache_hit - 0.9) < 1e-9


def test_get_stats_zero_cached_is_real_zero_hit(monkeypatch):
    """服务端真实返回 cached_tokens=0 → 0.0（真实零命中，前端显示「缓存 0%」）。"""
    from niu_api.compat import get_stats
    _patch_stats_env(monkeypatch, real_tokens=20000, cached_tokens=0)
    stats = asyncio.run(get_stats())
    assert stats.context_usage == 0.2
    assert stats.context_cache_hit == 0.0


def test_get_stats_none_when_unknown(monkeypatch):
    """服务端未返回 cached 字段（handler 值为 None）→ None，前端隐藏元素。"""
    from niu_api.compat import get_stats
    _patch_stats_env(monkeypatch, real_tokens=20000, cached_tokens=None)
    stats = asyncio.run(get_stats())
    assert stats.context_usage == 0.2
    assert stats.context_cache_hit is None


def test_get_stats_none_when_no_real_tokens(monkeypatch):
    """无真实 tokens（fallback 估算路径）→ None。"""
    from niu_api.compat import get_stats
    import niu_api.compat as compat
    _patch_stats_env(monkeypatch, real_tokens=0, cached_tokens=0)
    # 屏蔽 fallback 估算对结果无影响即可——只断言 cache_hit 为 None
    async def fake_estimate(store=None, context_window=None, messages=None):
        return 0.35
    monkeypatch.setattr(compat, "compute_context_usage_estimate", fake_estimate)
    stats = asyncio.run(get_stats())
    assert stats.context_cache_hit is None


# ---------------------------------------------------------------------------
# 4b. M2-F2 页面三级链：truth>0 > _fold_stats["usage"]（压实回填）> fallback 估算
#     子 Agent（agent= 参数）无此概念 → 0
# ---------------------------------------------------------------------------

class _CmStub:
    def __init__(self, fold_stats):
        self._fold_stats = fold_stats


def test_get_stats_tier2_reads_fold_stats_usage(monkeypatch):
    """主 Agent truth=0 + _fold_stats['usage']=0.365 → context_usage==0.365（不走 fallback）。"""
    from niu_api.compat import get_stats
    import niu_api.compat as compat
    import agent.context_manager as cm_mod
    _patch_stats_env(monkeypatch, real_tokens=0, cached_tokens=None)
    monkeypatch.setattr(cm_mod, "peek_context_manager",
                        lambda: _CmStub({"n": 1, "p": 5.0, "usage": 0.365}))

    def boom(*a, **k):
        raise AssertionError("fallback 估算不应被调用（回填值在场）")

    monkeypatch.setattr(compat, "compute_context_usage_estimate", boom)
    stats = asyncio.run(get_stats())
    assert stats.context_usage == 0.365


def test_get_stats_tier2_none_stats_falls_back(monkeypatch):
    """_fold_stats=None（CM 未初始化//new 已清）→ fallback 全库估算。"""
    from niu_api.compat import get_stats
    import niu_api.compat as compat
    import agent.context_manager as cm_mod
    _patch_stats_env(monkeypatch, real_tokens=0, cached_tokens=None)
    monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: None)

    async def fake_estimate(store=None, context_window=None, messages=None):
        return 0.35

    monkeypatch.setattr(compat, "compute_context_usage_estimate", fake_estimate)
    stats = asyncio.run(get_stats())
    assert stats.context_usage == 0.35


def test_get_stats_tier2_usage_none_falls_back(monkeypatch):
    """_fold_stats 在场但 usage=None（组装后未回填）→ fallback 估算。"""
    from niu_api.compat import get_stats
    import niu_api.compat as compat
    import agent.context_manager as cm_mod
    _patch_stats_env(monkeypatch, real_tokens=0, cached_tokens=None)
    monkeypatch.setattr(cm_mod, "peek_context_manager",
                        lambda: _CmStub({"n": 1, "p": 5.0, "usage": None}))

    async def fake_estimate(store=None, context_window=None, messages=None):
        return 0.35

    monkeypatch.setattr(compat, "compute_context_usage_estimate", fake_estimate)
    stats = asyncio.run(get_stats())
    assert stats.context_usage == 0.35


def test_get_stats_subagent_zero_without_truth(monkeypatch):
    """子 Agent（agent= 参数）无真值 → 0（三级链不适用，不读 _fold_stats）。"""
    from niu_api.compat import get_stats
    import niu_api.compat as compat
    import agent.context_manager as cm_mod
    _patch_stats_env(monkeypatch, real_tokens=0, cached_tokens=None)
    monkeypatch.setattr(cm_mod, "peek_context_manager",
                        lambda: _CmStub({"n": 1, "p": 5.0, "usage": 0.365}))

    def boom(*a, **k):
        raise AssertionError("子 Agent 路径不应触发 fallback 估算")

    monkeypatch.setattr(compat, "compute_context_usage_estimate", boom)
    stats = asyncio.run(get_stats(agent="some-sub-agent"))
    assert stats.context_usage == 0.0
