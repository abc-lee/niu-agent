"""压缩后刷新使用率：compute_context_usage_estimate 全量估算函数 +
M2-F2 压实真值回填 _fold_stats["usage"] 四出口锁（页面/动态块统一读回填值）。"""
import asyncio
from types import SimpleNamespace

import pytest

from niu_api import compat


class _Msg:
    def __init__(self, role, content, tool_calls=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls


@pytest.mark.asyncio
async def test_compute_usage_estimate_uses_db_estimate(monkeypatch):
    """无真实 tokens 时用全量 messages.db 估算 → usage = total/context_window"""
    msgs = [_Msg("user", "你好" * 100), _Msg("assistant", "回复" * 100)]

    async def _fake_get_store():
        return _FakeStore(msgs)

    monkeypatch.setattr(compat, "get_message_store", _fake_get_store)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 1000)

    usage = await compat.compute_context_usage_estimate()
    assert usage > 0.0  # 有消息必然 >0（compute 不 clamp，usage 可合法超 1.0，勿断言上界）


@pytest.mark.asyncio
async def test_compute_usage_estimate_empty_db(monkeypatch):
    """空 messages.db → usage = 0"""

    async def _fake_get_store():
        return _FakeStore([])

    monkeypatch.setattr(compat, "get_message_store", _fake_get_store)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 1000)

    usage = await compat.compute_context_usage_estimate()
    assert usage == 0.0


@pytest.mark.asyncio
async def test_compute_usage_estimate_error_safe(monkeypatch):
    """异常 → 返回 None 不抛出（调用方 or 0.0 兜底）"""
    def boom():
        raise RuntimeError("store down")

    monkeypatch.setattr(compat, "get_message_store", boom)

    usage = await compat.compute_context_usage_estimate()
    assert usage is None


@pytest.mark.asyncio
async def test_compute_usage_estimate_zero_window(monkeypatch):
    """context_window ≤ 0 → None（无效配置，不伪装 0%）"""

    async def _fake_get_store():
        return _FakeStore([])

    monkeypatch.setattr(compat, "get_message_store", _fake_get_store)
    monkeypatch.setattr(compat, "_read_context_window_tokens", lambda: 0)

    usage = await compat.compute_context_usage_estimate()
    assert usage is None


@pytest.mark.asyncio
async def test_compute_usage_estimate_injected_store(monkeypatch):
    """注入 store/context_window 时不重复扫描（get_stats 复用）"""
    msgs = [_Msg("user", "hi")]
    calls = []
    monkeypatch.setattr(compat, "get_message_store", lambda: calls.append("called") or _FakeStore([]))

    usage = await compat.compute_context_usage_estimate(store=_FakeStore(msgs), context_window=100)
    assert usage > 0.0
    assert calls == []  # 未调用 get_message_store（已注入）


@pytest.mark.asyncio
async def test_compute_usage_estimate_injected_messages(monkeypatch):
    """注入 messages 时不扫描 store（Task 3 传压缩后列表，避免二次全表扫描）"""
    msgs = [_Msg("user", "hi"), _Msg("assistant", "hello")]
    scanned = []

    class _ScanStore(_FakeStore):
        async def get_messages(self):
            scanned.append("scan")
            return await super().get_messages()

    async def _fake_get_store():
        return _ScanStore([])

    monkeypatch.setattr(compat, "get_message_store", _fake_get_store)

    usage = await compat.compute_context_usage_estimate(messages=msgs, context_window=1000)
    assert usage > 0.0  # 从注入列表算出
    assert scanned == []  # store.get_messages 未被调用（messages 已注入，无全表扫描）


class _FakeStore:
    def __init__(self, msgs):
        self._msgs = msgs

    async def get_messages(self):
        return self._msgs


class _FakeHandler:
    def __init__(self):
        self._last_prompt_tokens = 60000


class _FakeRunner:
    def __init__(self):
        self.handler = _FakeHandler()


def _patch_notify_env(monkeypatch, chat_mod):
    """mock runner/loop/broadcast；call_soon_threadsafe 签名 (self, fn, ev) 与真实一致"""
    fake_runner = _FakeRunner()
    monkeypatch.setattr(chat_mod, "get_runner", lambda: fake_runner)
    monkeypatch.setattr(chat_mod, "_main_loop",
                        type("L", (), {"is_closed": lambda self: False,
                                       "call_soon_threadsafe": lambda self, fn, ev: fn(ev)})())
    captured = {}
    monkeypatch.setattr(chat_mod, "_sync_broadcast", lambda ev: captured.update(ev))
    return fake_runner, captured


@pytest.mark.asyncio
async def test_notify_done_resets_prompt_tokens(monkeypatch):
    """done+reset_tokens=True：主 runner._last_prompt_tokens 置 0（旧值压缩后失效）"""
    from niu_api import chat as chat_mod

    fake_runner, captured = _patch_notify_env(monkeypatch, chat_mod)

    chat_mod.notify_compact_status_sync("done", mode="sleep", usage=0.3, reset_tokens=True)
    assert fake_runner.handler._last_prompt_tokens == 0
    assert captured == {"type": "compact_status", "status": "done", "mode": "sleep", "usage": 0.3}


@pytest.mark.asyncio
async def test_notify_done_without_reset_keeps_tokens(monkeypatch):
    """done 但 reset_tokens=False（未实际压缩的 skip 路径）：不置 0、usage 透传"""
    from niu_api import chat as chat_mod

    fake_runner, captured = _patch_notify_env(monkeypatch, chat_mod)

    chat_mod.notify_compact_status_sync("done", mode="sleep")
    assert fake_runner.handler._last_prompt_tokens == 60000  # 未动
    assert captured["usage"] is None


@pytest.mark.asyncio
async def test_notify_started_keeps_tokens(monkeypatch):
    """started 事件：不置 0、usage 透传为 None"""
    from niu_api import chat as chat_mod

    fake_runner, captured = _patch_notify_env(monkeypatch, chat_mod)

    chat_mod.notify_compact_status_sync("started", mode="sleep")
    assert fake_runner.handler._last_prompt_tokens == 60000  # 未动
    assert captured["usage"] is None


@pytest.mark.asyncio
async def test_notify_done_runner_none_defensive(monkeypatch):
    """get_runner 返回 None（runner 未初始化）：reset 块静默跳过、无广播、无副作用"""
    from niu_api import chat as chat_mod

    monkeypatch.setattr(chat_mod, "get_runner", lambda: None)
    monkeypatch.setattr(chat_mod, "_main_loop", None)  # 显式：loop 未启动 → 提前 return
    broadcast_calls = []
    monkeypatch.setattr(chat_mod, "_sync_broadcast", lambda ev: broadcast_calls.append(ev))

    chat_mod.notify_compact_status_sync("done", mode="sleep", reset_tokens=True)  # 不抛
    assert broadcast_calls == []  # _main_loop None → 未广播


@pytest.mark.asyncio
async def test_notify_done_runner_raises_defensive(monkeypatch):
    """get_runner 抛异常：reset 块 try/except 静默吞掉，事件仍正常广播（usage=None）"""
    from niu_api import chat as chat_mod

    def boom():
        raise RuntimeError("runner down")

    monkeypatch.setattr(chat_mod, "get_runner", boom)
    monkeypatch.setattr(chat_mod, "_main_loop",
                        type("L", (), {"is_closed": lambda self: False,
                                       "call_soon_threadsafe": lambda self, fn, ev: fn(ev)})())
    captured = {}
    monkeypatch.setattr(chat_mod, "_sync_broadcast", lambda ev: captured.update(ev))

    chat_mod.notify_compact_status_sync("done", mode="sleep", reset_tokens=True)  # 不抛
    assert captured["status"] == "done"  # 事件仍广播，usage 透传 None
    assert captured["usage"] is None


@pytest.mark.asyncio
async def test_post_compress_usage_deleted(monkeypatch):
    """消息数减少（实际压缩）→ (True, 估算 usage)"""
    from niu_api import compat as compat_mod

    # 锁 call-site 契约：messages=after（压缩后列表）与 store 必须传入（防回归成二次全表扫描）
    calls = []

    async def fake_compute(*a, **kw):
        calls.append(kw)
        return 0.3

    # 辅助函数内部只调一次 get_messages 作为"压缩后"状态——恒返回 1 条（压缩后），
    # msgs_before=2 参数即代表压缩前计数
    msgs = [_Msg("user", "a"), _Msg("assistant", "b")]

    class _ShrunkStore(_FakeStore):
        async def get_messages(self):
            return [self._msgs[0]]

    monkeypatch.setattr(compat_mod, "compute_context_usage_estimate", fake_compute)

    async def _fake_get_store():
        return _ShrunkStore(msgs)

    monkeypatch.setattr(compat_mod, "get_message_store", _fake_get_store)

    compressed, usage = await compat_mod._compute_post_compress_usage(store=None, msgs_before=2)
    assert compressed is True
    assert usage == 0.3
    assert calls, "compute_context_usage_estimate must be invoked on actual compression"
    assert calls[0]["messages"] == [msgs[0]]  # messages=after（压缩后列表），identity 成立
    assert calls[0]["store"]._msgs is msgs    # store 复用传入（未重新 get_message_store）


@pytest.mark.asyncio
async def test_post_compress_usage_unchanged(monkeypatch):
    """消息数不变（skip/未压缩）→ (False, None)"""
    from niu_api import compat as compat_mod

    async def _fake_get_store():
        return _FakeStore([_Msg("user", "a"), _Msg("assistant", "b")])

    monkeypatch.setattr(compat_mod, "get_message_store", _fake_get_store)
    compressed, usage = await compat_mod._compute_post_compress_usage(store=None, msgs_before=2)
    assert compressed is False
    assert usage is None


@pytest.mark.asyncio
async def test_tidy_finally_no_reset_when_skipped(monkeypatch, tmp_path):
    """sleep 管道未压缩（skip 场景）：finally 调 notify done 不带 usage、不 reset_tokens。

    真实路径：mode='sleep' + 低使用率（journal 腿被跳过）+ F1/F2 为空（entity/dream 无事可做）
    → 消息数不变 → _compute_post_compress_usage 判定未压缩 → done 不携带 usage、不 reset。
    """
    import pathlib as _pl

    from niu_api import chat as chat_mod
    from niu_api import compat as compat_mod

    calls = []
    monkeypatch.setattr(chat_mod, "notify_compact_status_sync",
                        lambda status, mode="", usage=None, reset_tokens=False:
                            calls.append((status, mode, usage, reset_tokens)))
    monkeypatch.setattr(chat_mod, "get_or_create_runner",
                        lambda: type("R", (), {"llm_config": {},
                                               "handler": None,
                                               "_ensure_session_chain": lambda self, max_days=10: None})())
    monkeypatch.setattr(chat_mod, "get_runner", lambda: None)
    monkeypatch.setattr("agent.subagent.call_subagent_with_auto_answer",
                        lambda *a, **kw: "SUBAGENT_ERROR: mocked")
    # 非空库（空库会提前 return "No messages to tidy"，测不到 skip 判定）
    async def _fake_get_store():
        return _FakeStore([_Msg("user", "a"), _Msg("assistant", "b")])

    monkeypatch.setattr(compat_mod, "get_message_store", _fake_get_store)

    # 低使用率（<50%）→ journal 腿跳过；游标/中继文件全部落在隔离 tmp 目录
    class _Calc:
        @staticmethod
        def count_message_single(role, content, tool_calls=None):
            return 10  # 2×10=20 tokens / 窗口 8000 → usage 远低于 50%

    monkeypatch.setattr("agent.token_calculator.TokenCalculator.get", lambda: _Calc())
    monkeypatch.setattr(compat_mod, "_read_context_window_tokens", lambda: 8000)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("os.path.expanduser",
                        lambda p: str(tmp_path / ".niu" / "isolated") if p.startswith("~") else p)
    monkeypatch.setattr("niu_api.compat.is_sleeping", lambda: True)
    empty_f1 = str(tmp_path / "f1.md")
    empty_f2 = str(tmp_path / "f2.md")
    monkeypatch.setattr("agent.md_mirror.F1_PATH", empty_f1)
    monkeypatch.setattr("agent.md_mirror.F2_PATH", empty_f2)

    result = await compat_mod._tidy_context_impl({"mode": "sleep"})

    assert result.get("status") == "ok", f"实际: {result}"
    done_calls = [c for c in calls if c[0] == "done"]
    assert len(done_calls) == 1, f"expected single done broadcast, got calls={calls}"
    assert done_calls[-1][1] == "sleep"
    assert done_calls[-1][2] is None      # 未压缩 → 未推 usage
    assert done_calls[-1][3] is False     # 未 reset_tokens


# ---------------------------------------------------------------------------
# M2-F2 压实真值回填 _fold_stats["usage"]——四出口锁（页面三级链/动态块统一读它）
# ---------------------------------------------------------------------------

@pytest.fixture
def _cfg(tmp_path, monkeypatch):
    """user-config 重定向到 tmp 文件（缺省空配置=全默认；隔离真实 ~/.niu）。"""
    import json as _json
    from niu_api import config as cfg_mod
    p = tmp_path / "user-config.json"
    p.write_text(_json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", str(p))
    return p


def _cm_stub(usage=0.9):
    """ContextManager 桩：只承载 _fold_stats（回填目标是这个 dict）。"""
    return SimpleNamespace(_fold_stats={"n": 1, "p": 5.0, "usage": usage})


def _stats(usage):
    return {"keep_turns": 1, "blocks_archived": 2, "tools_placeholderized": 0,
            "tokens_estimate": 4200, "context_window": 100000, "usage": usage}


def test_backfill_exit1_inloop_high_usage(monkeypatch, _cfg):
    """出口① in-loop（runner._on_context_high_usage 压实成功）→ _fold_stats['usage']==stats usage。"""
    import agent.context_manager as cm_mod
    from agent.context_assembler.compaction import AUTO_GATE
    from agent.runner import NiuRunner

    cm = _cm_stub()
    monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
    r = NiuRunner.__new__(NiuRunner)
    monkeypatch.setattr(NiuRunner, "_sync_get_messages", lambda self, limit=None: [object(), object()])
    new_view = [{"role": "system", "content": "S"}, {"role": "user", "content": "IDX"}]
    monkeypatch.setattr("agent.context_assembler.compaction.build_compact_view",
                        lambda messages, **kw: (new_view, _stats(0.42)))
    AUTO_GATE.release()
    try:
        messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}]
        assert r._on_context_high_usage(messages, 90000, 100000) is True
        assert cm._fold_stats["usage"] == 0.42
        # 原地回写生效（同列表对象内容替换）
        assert [e["content"] for e in messages] == ["S", "IDX"]
    finally:
        AUTO_GATE.release()


def test_backfill_exit1_cm_none_guard(monkeypatch, _cfg):
    """双守卫①：peek→None（ContextManager 未初始化）→ 压实路径不 TypeError、正常完成。"""
    import agent.context_manager as cm_mod
    from agent.context_assembler.compaction import AUTO_GATE
    from agent.runner import NiuRunner

    monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: None)
    r = NiuRunner.__new__(NiuRunner)
    monkeypatch.setattr(NiuRunner, "_sync_get_messages", lambda self, limit=None: [object()])
    monkeypatch.setattr("agent.context_assembler.compaction.build_compact_view",
                        lambda messages, **kw: ([{"role": "system", "content": "S"}], _stats(0.42)))
    AUTO_GATE.release()
    try:
        assert r._on_context_high_usage([{"role": "system", "content": "S"}], 90000, 100000) is True
    finally:
        AUTO_GATE.release()


def test_backfill_exit1_fold_stats_none_guard(monkeypatch, _cfg):
    """双守卫②：_fold_stats=None（/new 已清）→ 回填跳过不 TypeError。"""
    import agent.context_manager as cm_mod
    from agent.context_assembler.compaction import AUTO_GATE
    from agent.runner import NiuRunner

    cm = SimpleNamespace(_fold_stats=None)
    monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)
    r = NiuRunner.__new__(NiuRunner)
    monkeypatch.setattr(NiuRunner, "_sync_get_messages", lambda self, limit=None: [object()])
    monkeypatch.setattr("agent.context_assembler.compaction.build_compact_view",
                        lambda messages, **kw: ([{"role": "system", "content": "S"}], _stats(0.42)))
    AUTO_GATE.release()
    try:
        assert r._on_context_high_usage([{"role": "system", "content": "S"}], 90000, 100000) is True
        assert cm._fold_stats is None  # 未回写僵尸值
    finally:
        AUTO_GATE.release()


@pytest.mark.asyncio
async def test_backfill_exit2_manual_compact_page_same_source(monkeypatch, _cfg):
    """出口② 手动 /compact（_compact_context_impl）→ 回填；页面 get_stats（truth=0）读同值（R3-B P2-1）。"""
    import niu_api.compat as compat_mod
    import niu_api.chat as chat_mod
    import agent.context_manager as cm_mod

    cm = _cm_stub()
    monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)

    class _Store:
        async def count_messages(self):
            return 3

    async def fake_store():
        return _Store()

    monkeypatch.setattr(compat_mod, "get_message_store", fake_store)

    async def fake_compact(store):
        return ([], _stats(0.31))

    monkeypatch.setattr("agent.context_assembler.compaction.compact_now_detailed", fake_compact)
    events = []
    monkeypatch.setattr(chat_mod, "notify_compact_status_sync",
                        lambda status, mode="", usage=None, reset_tokens=False:
                            events.append((status, mode, usage, reset_tokens)))

    result = await compat_mod._compact_context_impl({"session_id": "s1"})
    assert result["status"] == "ok" and result["usage"] == 0.31
    assert cm._fold_stats["usage"] == 0.31
    # done 广播带 usage + reset_tokens（圆环跳变协议不变）
    assert ("done", "compact", 0.31, True) in events

    # 页面同源：压实后真值已清 0（reset_tokens）→ 三级链 tier2 读回填值，不走 fallback 估算
    monkeypatch.setattr(compat_mod, "_read_context_window_tokens", lambda: 100000)

    class _Runner:
        handler = SimpleNamespace(_last_prompt_tokens=0, _last_cached_tokens=None)

    monkeypatch.setattr(chat_mod, "get_or_create_runner", lambda: _Runner())

    def boom(*a, **k):
        raise AssertionError("fallback 估算不应被调用（回填值在场）")

    monkeypatch.setattr(compat_mod, "compute_context_usage_estimate", boom)
    stats_resp = await compat_mod.get_stats()
    assert stats_resp.context_usage == 0.31


@pytest.mark.asyncio
async def test_backfill_exit3_overflow_fire_and_forget(monkeypatch, _cfg):
    """出口③ CONTEXT_OVERFLOW（fire_and_forget_compaction）→ 回填 + done 广播。"""
    import niu_api.chat as chat_mod
    import agent.context_manager as cm_mod

    cm = _cm_stub()
    monkeypatch.setattr(cm_mod, "peek_context_manager", lambda: cm)

    async def fake_compact(store):
        return ([], _stats(0.27))

    monkeypatch.setattr("agent.context_assembler.compaction.compact_now_detailed", fake_compact)
    events = []
    monkeypatch.setattr(chat_mod, "notify_compact_status_sync",
                        lambda status, mode="", usage=None, reset_tokens=False:
                            events.append((status, usage, reset_tokens)))

    chat_mod.fire_and_forget_compaction(object(), source="test")
    # task 调度在当前 loop——让出直至 done 到达（有界等待，禁无限轮询）
    for _ in range(100):
        if any(e[0] == "done" for e in events):
            break
        await asyncio.sleep(0)

    assert cm._fold_stats["usage"] == 0.27
    assert ("done", 0.27, True) in events


@pytest.mark.asyncio
async def test_backfill_exit4_assembly_exit(tmp_path, monkeypatch, _cfg):
    """出口④ 组装出口行为锁：高水位 DB + AUTO_GATE 复位 → get_context_for_chat 压实后
    _fold_stats['usage']==stats['usage']（页面三级链/动态块随后统一读它）。"""
    import agent.context_manager as cm_mod
    import agent.context_assembler.calibration as calibration
    from agent.context_assembler.compaction import AUTO_GATE
    from agent.context_manager import ContextManager
    from agent.session import MessageStore

    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.0
    try:
        store = MessageStore(str(tmp_path / "m.db"))
        await store.init_db()
        # 高水位：est≈2008（确定性计数）> max_tokens=1000 × trigger(0.80)
        await store.add_message(role="user", content="Q" * 2000)
        cm = ContextManager(store, max_tokens=1000, blocks_db_path=tmp_path / "b.db")
        monkeypatch.setattr(ContextManager, "count_tokens_simple",
                            staticmethod(lambda messages: sum(
                                len(m.get("content", "")) + 8 for m in messages)))
        new_view = [{"role": "system", "content": "S"}]
        monkeypatch.setattr("agent.context_assembler.compaction.build_compact_view",
                            lambda messages, **kw: (new_view, _stats(0.42)))
        AUTO_GATE.release()
        try:
            view = await cm.get_context_for_chat(exclude_last=False)
            assert view == new_view  # 压实路径生效（非未压实视图）
            assert cm._fold_stats is not None
            assert cm._fold_stats["usage"] == 0.42
        finally:
            AUTO_GATE.release()
    finally:
        calibration._cached_ratio = old_ratio


@pytest.mark.asyncio
async def test_assembly_exit_fold_stats_none_guard(tmp_path, monkeypatch, _cfg):
    """双守卫：_fold_stats=None（陈旧路径）时组装出口压实回填不 TypeError、无僵尸回写。"""
    import agent.context_manager as cm_mod
    import agent.context_assembler.calibration as calibration
    from agent.context_assembler.compaction import AUTO_GATE
    from agent.context_manager import ContextManager
    from agent.session import MessageStore

    old_ratio = calibration._cached_ratio
    calibration._cached_ratio = 1.0
    try:
        store = MessageStore(str(tmp_path / "m.db"))
        await store.init_db()
        await store.add_message(role="user", content="Q" * 2000)
        cm = ContextManager(store, max_tokens=1000, blocks_db_path=tmp_path / "b.db")
        monkeypatch.setattr(ContextManager, "count_tokens_simple",
                            staticmethod(lambda messages: sum(
                                len(m.get("content", "")) + 8 for m in messages)))

        # 模拟陈旧路径：assemble_view_sync 返回高水位视图但不置 _fold_stats（保持 None）
        def fake_assemble(self, db_messages, exclude_last=True):
            return [{"role": "user", "content": "V" * 2000}]

        monkeypatch.setattr(cm, "assemble_view_sync", fake_assemble)
        assert cm._fold_stats is None
        new_view = [{"role": "system", "content": "S"}]
        monkeypatch.setattr("agent.context_assembler.compaction.build_compact_view",
                            lambda messages, **kw: (new_view, _stats(0.42)))
        AUTO_GATE.release()
        try:
            view = await cm.get_context_for_chat(exclude_last=False)  # 不得抛 TypeError
            assert view == new_view
            assert cm._fold_stats is None  # 无僵尸回写
        finally:
            AUTO_GATE.release()
    finally:
        calibration._cached_ratio = old_ratio
