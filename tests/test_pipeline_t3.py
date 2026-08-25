"""T3 整理管道队列测试：_execute_force_pipeline 提取 + 入口 8（内联直调 + 转换块）。

设计见 docs/superpowers/plans/2026-08-23-remove-outer-subagent-timeouts.md §3.3（入口 8 内联化，
不经队列、无外层等待上限）与 docs/superpowers/plans/2026-08-20-tidy-pipeline-queue.md §3.1 入口 9。
全 mock：runner._execute_force_pipeline（不真实调压缩管道）+ 转换块 DB 重载（_sync_get_messages 假消息）——
禁真实 LLM、禁图谱写入、messages.db 零新增（转换块孤立 tool 清理路径用无 tool_calls 假消息绕过）。
"""
import asyncio

import pytest

import niu_api.chat as chat_module
import niu_api.compat as compat
from agent.runner import NiuRunner
from niu_api.compat import start_pipeline_queue, stop_pipeline_queue


class _FakeDbMsg:
    """模拟 DB Message 对象（转换块用 getattr 访问 role/content/tool_calls/tool_call_id/id）。"""

    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls if tool_calls is not None else []
        self.tool_call_id = tool_call_id


def _make_runner():
    """NiuRunner.__new__ 实例，仅赋值测试所需属性（禁真实 __init__/LLM/DB）。"""
    runner = NiuRunner.__new__(NiuRunner)
    runner.llm_config = {}
    runner.default_model = ""
    runner._assemble_system_message = lambda *a, **k: None  # 转换块 system 重建 mock（不真组装）
    return runner


@pytest.fixture(autouse=True)
async def _clean_pipeline():
    """每个用例前复位全局队列/去重表/精灵状态（模块级全局，避免用例间串扰）。"""
    if compat._pipeline_queue is not None:
        await stop_pipeline_queue()
    compat._SPIRIT_STATE = "idle"
    yield
    if compat._pipeline_queue is not None:
        await stop_pipeline_queue()


# ---------------------------------------------------------------------------
# 入口 8：runner-force 内联直调（Case 2：不经队列、无外层等待上限）+ 转换块
# ---------------------------------------------------------------------------

def _release_auto_gate():
    """测试间复位全局滞回闸门（防跨用例闩锁污染）。"""
    from agent.context_assembler.compaction import AUTO_GATE
    AUTO_GATE.release()


def test_entry8_inline_direct_call_then_conversion_block(monkeypatch):
    """直调契约：机械压实被回调直接同步调用、零队列投递；新视图原地回写（Task 3 收编）。"""
    from agent.context_assembler import compaction

    _release_auto_gate()
    runner = _make_runner()
    calls = []

    def fake_compact(db_messages, *, system_msg=None, **kw):
        calls.append((len(db_messages), system_msg is not None))
        return [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "[历史索引] 共 1 块早期对话已归档"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ], {"usage": 0.35, "keep_turns": 3, "blocks_archived": 1,
            "tools_placeholderized": 0}

    monkeypatch.setattr(compaction, "build_compact_view", fake_compact)
    runner._sync_get_messages = lambda: [
        _FakeDbMsg("s1", "system", "system prompt"),
        _FakeDbMsg("m1", "user", "hello"),
        _FakeDbMsg("m2", "assistant", "hi there"),
    ]

    enqueue_calls: list[tuple] = []
    monkeypatch.setattr(
        compat, "_pipeline_enqueue",
        lambda kind, request=None, held=False: enqueue_calls.append((kind, request, held)),
    )
    start_pipeline_queue()  # 队列可用也应零投递（机械压实不经队列）

    messages = [{"role": "system", "content": "system prompt"}]
    result = runner._on_context_high_usage(messages, 180000, 200000)

    assert calls == [(3, True)]  # 直调完成——回调返回前压实已执行完，system 原样传入
    assert enqueue_calls == [], f"不应有任何队列投递，实际 {enqueue_calls}"
    assert result is None
    # 回写格式契约：每条 dict + role/content 键 + system 保留在 messages[0]
    assert len(messages) == 4, "压实视图应原地回写"
    assert all(isinstance(m, dict) for m in messages)
    assert all("role" in m and "content" in m for m in messages)
    assert messages[0].get("role") == "system"
    assert messages[1]["content"].startswith("[历史索引]")


def test_entry8_gate_latched_skips_compaction(monkeypatch):
    """滞回闸门闩锁中：回调跳过压实、零投递、messages 不动（双触发去重语义）。"""
    from agent.context_assembler import compaction

    from niu_api.compat import start_pipeline_queue as _start  # noqa: F401

    _release_auto_gate()
    from agent.context_assembler.compaction import AUTO_GATE
    AUTO_GATE.try_acquire(0.9)  # 预先闩锁

    runner = _make_runner()
    compact_spy = []

    def fake_compact(*a, **kw):
        compact_spy.append(1)
        return [], {}

    monkeypatch.setattr(compaction, "build_compact_view", fake_compact)
    runner._sync_get_messages = lambda: [_FakeDbMsg("m1", "user", "hello")]

    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "old"}]
    result = runner._on_context_high_usage(messages, 190000, 200000)

    assert result is None
    assert compact_spy == [], "闸门闩锁中不得重复压实"
    assert messages == [  # 未压实 → 原列表保持
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "old"},
    ]


def test_entry8_no_queue_dependency(monkeypatch):
    """无队列依赖：全局整理队列未创建时机械压实照常工作并完成回写广播。"""
    from agent.context_assembler import compaction

    _release_auto_gate()
    runner = _make_runner()

    def fake_compact(db_messages, *, system_msg=None, **kw):
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "[历史索引]"},
            {"role": "user", "content": "hello"},
        ], {"usage": 0.30, "keep_turns": 2, "blocks_archived": 1,
            "tools_placeholderized": 0}

    monkeypatch.setattr(compaction, "build_compact_view", fake_compact)
    runner._sync_get_messages = lambda: [_FakeDbMsg("m1", "user", "hello")]
    assert compat._pipeline_queue is None  # 队列未创建（fixture 复位）

    broadcasts = []
    monkeypatch.setattr(
        "niu_api.chat.notify_compact_status_sync",
        lambda status, **k: broadcasts.append(status),
    )

    messages = [{"role": "system", "content": "sys"}]
    result = runner._on_context_high_usage(messages, 180000, 200000)  # 真值比率 90% 过闸门

    assert result is None
    assert broadcasts == ["started", "done"], "started/done 事件均须广播（防前端圆环卡死）"
    assert len(messages) == 3 and all(isinstance(m, dict) for m in messages)
    assert messages[0].get("role") == "system"


async def test_enqueue_failure_reraises(monkeypatch):
    """compat _pipeline_enqueue 投递失败（put_nowait 抛异常）：重新 raise（调用方契约是返回 Future）。

    T6：force/runner-force 去重表已退役，仅剩「异常重新抛出」语义。
    """
    start_pipeline_queue()
    q = compat._pipeline_queue

    def _boom_put(*a, **k):
        raise RuntimeError("queue closed")

    monkeypatch.setattr(q, "put_nowait", _boom_put)
    with pytest.raises(RuntimeError, match="queue closed"):
        compat._pipeline_enqueue("sleep", {"mode": "sleep", "session_id": "s"}, held=False)
