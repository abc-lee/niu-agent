"""T5 测试：sleep 状态机检查点 CP1-CP3（nap/force/runner-force 零插入）。

设计见 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §4.2 / §5 T5 / §6 T5。
全 mock：call_subagent_with_auto_answer / 游标文件 / is_sleeping / runner——禁真实 LLM、禁图谱写入、messages.db 零新增。

检查点契约（仅 mode=='sleep'，§4.2）：
- CP0 worker 取出 sleep 任务执行前：非睡眠 → {"status":"cancelled","reason":"woke_up"}，impl 零调用
- CP1 entity 段完成后：非睡眠 → {"status":"interrupted","reason":"woke_up"}，dream/cm 不执行
- CP2 dream 段完成后：同上，cm 不执行
- CP3 compress 段入口（journal 两路汇合后单点）：同上，cm 不执行
- 已推进游标不回滚（entity/dream 游标保留 CP 打断时的推进值，下次续跑）
- nap/force/runner-force 零插入（反向断言：mock is_sleeping 抛异常 → 这些路径必须不触发）
"""
import asyncio
import json
import threading
from contextlib import ExitStack
from unittest import mock

import pytest

import niu_api.compat as compat


@pytest.fixture(autouse=True)
async def _clean_pipeline():
    """每个用例前复位全局队列/去重表/精灵状态（模块级全局，避免用例间串扰）。"""
    if compat._pipeline_queue is not None:
        await compat.stop_pipeline_queue()
    compat._active_compress_futs.clear()
    compat._SPIRIT_STATE = "idle"
    yield
    if compat._pipeline_queue is not None:
        await compat.stop_pipeline_queue()


NORMAL_JSON = json.dumps({"ok": True})  # 非 overflow / 非 incomplete / 非 failure 的正常返回


class _FakeCalc:
    def count_message_single(self, role, content, tool_calls=None):
        return 100


class _FakeRunner:
    def __init__(self):
        self.llm_config = {"model": "m", "apikey": "x", "apibase": "http://x"}
        self.handler = mock.MagicMock()
        self.handler._last_prompt_tokens = 0

    def _ensure_session_chain(self, max_days: int = 10) -> None:
        # dream 阶段收尾补链（真函数依赖 LightRAG，测试桩空操作）
        return None


def _tidy_messages():
    msgs = []
    for i, mid in enumerate(["m1", "m2"]):
        m = mock.MagicMock()
        m.id = mid
        m.role = "user"
        m.content = f"hello {i}"
        m.tool_calls = None
        m.tool_call_id = None
        msgs.append(m)
    return msgs


def _cp_patches(sleep_side_effect, call_mock):
    """T5 独立 fixture（同 test_subagent_failure_cursor._tidy_failure_patches 模式，各 Task fixture 独立）。

    - is_sleeping → sleep_side_effect（检查点翻转驱动）
    - call_subagent_with_auto_answer → call_mock（记录被调用 agent 顺序）
    - 四个游标文件 READ 强制 cursor=''（Path.exists→False → 游标留空）
    - _write_cursor_with_lock → MagicMock（记录调用，测试 hermetic）
    """
    return [
        mock.patch("agent.token_calculator.TokenCalculator.get", return_value=_FakeCalc()),
        mock.patch("niu_api.compat._read_context_window_tokens", return_value=8000),
        mock.patch("niu_api.chat.get_or_create_runner", return_value=_FakeRunner()),
        mock.patch("agent.subagent.call_subagent_with_auto_answer", call_mock),
        # builder refetch lightrag 段——mock 隔离，不读真实用户配置
        mock.patch("niu_api.llm_proxy.get_llm_config", return_value={
            "model": "test-model", "apikey": "test-key", "apibase": "https://test.example.com",
            "type": "openai", "provider": "", "reasoning_effort": "", "litellm_kwargs": {},
        }),
        mock.patch("niu_api.compat._read_protect_recent_count", return_value=0),
        mock.patch("niu_api.compat._read_warning_threshold", return_value=0.8),
        # 四个游标文件 READ 强制 cursor=''：Path.exists→False（缺失文件 → 游标留空）。
        # compat.py 在函数内 `from pathlib import Path`，无模块级 Path，故 patch 类方法本身
        mock.patch("pathlib.Path.exists", return_value=False),
        mock.patch("niu_api.compat._write_cursor_with_lock"),
        mock.patch("niu_api.compat.is_sleeping", side_effect=sleep_side_effect),
    ]


def _run_sleep_tidy(sleep_side_effect, call_mock=None):
    """直接调 _tidy_context_impl sleep 分支（绕过 worker/CP0），驱动 CP1-CP3。"""
    from niu_api.compat import _tidy_context_impl

    store = mock.MagicMock()
    store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
    if call_mock is None:
        call_mock = mock.MagicMock()
        call_mock.return_value = NORMAL_JSON
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in _cp_patches(sleep_side_effect, call_mock):
            stack.enter_context(p)
        write_mock = stack.enter_context(mock.patch("niu_api.compat._write_cursor_with_lock"))
        result = asyncio.run(_tidy_context_impl({"mode": "sleep", "session_id": "t"}, chat_lock_already_held=True))
    return result, write_mock, call_mock


def _called_agents(call_mock):
    return [c.kwargs.get("agent_name") for c in call_mock.call_args_list]


def _cursor_writes(write_mock):
    return [call.args[1] for call in write_mock.call_args_list]


# ---------------------------------------------------------------------------
# CP1-CP3：各检查点打断后后续步骤不执行、已推进游标不回滚
# ---------------------------------------------------------------------------

def test_cp1_interrupt_after_entity():
    """CP1：entity 段完成后唤醒 → interrupted；dream/cm 不执行；entity 游标推进不回滚。"""
    calls = {"n": 0}

    def sleep_side_effect():
        calls["n"] += 1
        return False  # 模拟 entity 期间被唤醒 → CP1 即断

    result, write_mock, call_mock = _run_sleep_tidy(sleep_side_effect)
    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock) == ["entity-extractor"], "dream/cm 不应执行"
    writes = _cursor_writes(write_mock)
    entity_writes = [d for d in writes if d.get("last_entity_extract_id")]
    assert entity_writes, "entity 游标应已推进"
    assert entity_writes[-1]["last_entity_extract_id"] == "m2"  # 已推进值不回滚
    assert [d for d in writes if d.get("last_dream_evolve_id")] == []
    assert calls["n"] == 1  # 仅 CP1 检查一次


def test_cp2_interrupt_after_dream():
    """CP2：dream 段完成后唤醒 → interrupted；cm 不执行；entity+dream 游标推进不回滚。"""
    calls = {"n": 0}

    def sleep_side_effect():
        calls["n"] += 1
        return calls["n"] < 2  # CP1 仍睡眠，CP2 断

    result, write_mock, call_mock = _run_sleep_tidy(sleep_side_effect)
    assert result == {"status": "interrupted", "reason": "woke_up"}
    assert _called_agents(call_mock) == ["entity-extractor", "dream-evolver"], "cm 不应执行"
    writes = _cursor_writes(write_mock)
    entity_writes = [d for d in writes if d.get("last_entity_extract_id")]
    dream_writes = [d for d in writes if d.get("last_dream_evolve_id")]
    assert entity_writes[-1]["last_entity_extract_id"] == "m2"  # 不回滚
    assert dream_writes[-1]["last_dream_evolve_id"] == "m2"  # 不回滚
    assert [d for d in writes if d.get("last_compress_id")] == []
    assert calls["n"] == 2


def test_cp3_interrupt_before_compress():
    """CP3：compress 段入口（journal 两路汇合后单点）唤醒 → interrupted；cm 不执行。"""
    calls = {"n": 0}

    def sleep_side_effect():
        calls["n"] += 1
        return calls["n"] < 3  # CP1/CP2 仍睡眠，CP3 断

    result, write_mock, call_mock = _run_sleep_tidy(sleep_side_effect)
    assert result == {"status": "interrupted", "reason": "woke_up"}
    # journal 此 fixture usage 2.5% < 50% 走 skipped 分支——CP3 在两路汇合后单点覆盖
    assert _called_agents(call_mock) == ["entity-extractor", "dream-evolver"], "cm 不应执行"
    writes = _cursor_writes(write_mock)
    assert [d for d in writes if d.get("last_entity_extract_id")][-1]["last_entity_extract_id"] == "m2"
    assert [d for d in writes if d.get("last_dream_evolve_id")][-1]["last_dream_evolve_id"] == "m2"
    assert [d for d in writes if d.get("last_compress_id")] == []
    assert calls["n"] == 3


def test_sleep_full_run_not_interrupted_when_asleep():
    """对照：全程睡眠 → 完整跑完（fixture 非空洞，CP 断言有判别力）。"""
    result, write_mock, call_mock = _run_sleep_tidy(lambda: True)
    assert result.get("status") == "ok", f"睡眠中不应打断: {result}"
    assert _called_agents(call_mock) == ["entity-extractor", "dream-evolver", "context-manager"]
    compress_writes = [d for d in _cursor_writes(write_mock) if d.get("last_compress_id")]
    assert compress_writes, "正常完成应推进压缩游标"


# ---------------------------------------------------------------------------
# CP0：worker 层排队唤醒检查（T2 已实现，T5 补契约测试）
# ---------------------------------------------------------------------------

async def test_cp0_worker_cancel_when_not_sleeping(monkeypatch):
    """CP0：worker 取出 sleep 任务时非睡眠 → cancelled/woke_up，impl 零调用。"""
    called = []

    async def fake_impl(request, chat_lock_already_held=False):
        called.append(request)
        return {"status": "ok"}

    monkeypatch.setattr(compat, "_tidy_context_impl", fake_impl)
    monkeypatch.setattr(compat, "is_sleeping", lambda: False)
    compat.start_pipeline_queue()
    try:
        fut = compat._pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)
        result = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=1.0)
    finally:
        await compat.stop_pipeline_queue()
    assert result == {"status": "cancelled", "reason": "woke_up"}
    assert called == [], f"CP0 cancelled 时 impl 不应被调用: {called}"


# ---------------------------------------------------------------------------
# 零插入反向断言：nap/force/runner-force 路径不得调用 is_sleeping
# ---------------------------------------------------------------------------

def _boom():
    """反向断言哨兵：is_sleeping 被调用即抛错 → 该路径存在零插入违反。"""
    raise AssertionError("该路径不应调用 is_sleeping（nap/force/runner-force 零插入）")


def test_force_never_calls_is_sleeping():
    """force 零插入：is_sleeping mock 抛异常 → force 路径不触发（反向断言）。"""
    from niu_api.compat import _tidy_context_impl

    store = mock.MagicMock()
    store.get_messages = mock.AsyncMock(return_value=_tidy_messages())
    call_mock = mock.MagicMock()
    call_mock.return_value = NORMAL_JSON
    with ExitStack() as stack:
        stack.enter_context(mock.patch("niu_api.compat.get_message_store", new=mock.AsyncMock(return_value=store)))
        for p in _cp_patches(_boom, call_mock):
            stack.enter_context(p)
        result = asyncio.run(_tidy_context_impl(
            {"mode": "force", "skip_compress": True, "session_id": "t"},
            chat_lock_already_held=True,
        ))
    assert result.get("status") == "ok", f"force 应正常跑完: {result}"
    assert _called_agents(call_mock) == ["entity-extractor", "dream-evolver", "journal-agent"]


async def test_nap_never_calls_is_sleeping(monkeypatch):
    """nap 零插入：is_sleeping mock 抛异常 → nap 路径不触发（反向断言）。"""
    class _FakeNapRunner:
        def __init__(self):
            self._nap_running = threading.Event()
            self.called = False

        def _run_nap_background(self):
            self.called = True
            return None

    fake = _FakeNapRunner()
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: fake)
    monkeypatch.setattr(compat, "is_sleeping", _boom)
    compat.start_pipeline_queue()
    try:
        fut = compat._pipeline_enqueue("nap", {}, held=False)
        result = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=1.0)
    finally:
        await compat.stop_pipeline_queue()
    assert result == {"status": "ok"}
    assert fake.called
    assert not fake._nap_running.is_set()  # worker finally 清 _nap_running


async def test_runner_force_never_calls_is_sleeping(monkeypatch):
    """runner-force 零插入：is_sleeping mock 抛异常 → runner-force 路径不触发（反向断言）。"""
    class _FakeForceRunner:
        def __init__(self):
            self.called = False

        def _execute_force_pipeline(self):
            self.called = True
            return {"status": "ok"}

    fake = _FakeForceRunner()
    monkeypatch.setattr("niu_api.chat.get_or_create_runner", lambda: fake)
    monkeypatch.setattr(compat, "is_sleeping", _boom)
    compat.start_pipeline_queue()
    try:
        fut = compat._pipeline_enqueue("runner-force", {}, held=False)
        result = await asyncio.wait_for(asyncio.wrap_future(fut), timeout=1.0)
    finally:
        await compat.stop_pipeline_queue()
    assert result == {"status": "ok"}
    assert fake.called
