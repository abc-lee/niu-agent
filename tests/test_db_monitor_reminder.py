"""db_monitor 第三链路 _remind_pending_asks（等待状态机）单元测试。

确定性设计：直接调 _remind_pending_asks(now=...) 函数级断言——
now 参数注入代替真实时钟，每子 Agent 30s 节流可精确复现（不依赖 time.sleep/真实时钟）。

依赖全部 patch 在**源模块**上（_remind_pending_asks 函数内 from ... import 从源模块取属性）：
- agent.ask_main_agent.get_pending_ask_registry（注册表 → waiting_unique_names 数据源）
- niu_api.compat._chat_lock（主 Agent 忙判断）
- agent.main_agent_request_queue.get_main_agent_request_queue（队列非空守卫）
- niu_api.chat.notify_new_message_sync（推送通道断言）
"""
import asyncio

import agent.ask_main_agent as ask_mod
import agent.main_agent_request_queue as q_mod
import niu_api.chat as chat_mod
import niu_api.compat as compat_mod

from agent.ask_main_agent import PendingAskRegistry
from niu_api import db_monitor


class _FakeLock:
    """模拟 asyncio.Lock 的 locked() 检查（测试不需要真实事件循环）。"""

    def __init__(self, locked=False):
        self._locked = locked

    def locked(self):
        return self._locked


class _FakeQueue:
    """模拟 MainAgentRequestQueue 的 is_empty() 检查（队列非空守卫）。"""

    def __init__(self, is_empty=True):
        self._is_empty = is_empty

    def is_empty(self):
        return self._is_empty


def _setup(monkeypatch, registry=None, chat_locked=False, queue_empty=True, notify_result=True):
    """组装 _remind_pending_asks 的依赖并返回推送捕获列表。

    所有 patch 均为 monkeypatch 自动回滚，_last_remind 由各用例显式清理。
    notify_result: fake_notify 的返回值（False 模拟推送失败——主 loop 不可用/无订阅者）。
    """
    monkeypatch.setattr(ask_mod, "get_pending_ask_registry", lambda: registry)
    monkeypatch.setattr(compat_mod, "_chat_lock", _FakeLock(chat_locked))
    monkeypatch.setattr(q_mod, "get_main_agent_request_queue", lambda: _FakeQueue(queue_empty))
    pushed = []

    def fake_notify(_message_id, role, content, source="electron"):
        pushed.append((role, content, source))
        return notify_result

    monkeypatch.setattr(chat_mod, "notify_new_message_sync", fake_notify)
    return pushed


def test_remind_pushes_when_waiting_and_main_agent_idle(monkeypatch):
    """waiting 列表非空 + 主 Agent 闲置 + 队列已空 → 推提醒。

    断言 content 含 【子Agent等待提醒】 + @{name}，且走 subagent_msg/source=subagent 通道
    （与链路 A 同款通道 → 前端 → session → 主 Agent LLM 新一轮）。
    """
    db_monitor._last_remind.clear()
    reg = PendingAskRegistry()
    reg.register("sub-remind-0001")
    pushed = _setup(monkeypatch, registry=reg)

    db_monitor._remind_pending_asks(now=1000.0)

    assert len(pushed) == 1
    role, content, source = pushed[0]
    assert role == "subagent_msg"
    assert source == "subagent"
    assert "【子Agent等待提醒】" in content
    assert "sub-remind-0001" in content
    assert "@sub-remind-0001" in content
    # 节流记录已写入（下一次同一子 Agent 30s 内不推）
    assert db_monitor._last_remind.get("sub-remind-0001") == 1000.0


def test_remind_throttled_30s_per_subagent(monkeypatch):
    """30s 节流：同一子 Agent 30s 内不重复推（now 注入确定性）。

    29s 后再次调用 → 不推；满 30s → 再次推。
    """
    db_monitor._last_remind.clear()
    reg = PendingAskRegistry()
    reg.register("sub-throttle")
    pushed = _setup(monkeypatch, registry=reg)

    db_monitor._remind_pending_asks(now=1000.0)
    assert len(pushed) == 1

    # 29s 后——节流内不推
    db_monitor._remind_pending_asks(now=1029.0)
    assert len(pushed) == 1

    # 满 30s——再次推
    db_monitor._remind_pending_asks(now=1030.0)
    assert len(pushed) == 2
    assert db_monitor._last_remind.get("sub-throttle") == 1030.0


def test_remind_stops_after_future_resolved(monkeypatch):
    """future 解除（set_answer）后 waiting 不含 → 停止提醒 + 清理 _last_remind。

    主 Agent 回复 @子名 → 链路 B set_answer → future 从注册表移除 → 提醒自动停止（闭环）。
    """
    db_monitor._last_remind.clear()
    reg = PendingAskRegistry()
    reg.register("sub-resolved")
    pushed = _setup(monkeypatch, registry=reg)

    db_monitor._remind_pending_asks(now=1000.0)
    assert len(pushed) == 1
    assert db_monitor._last_remind.get("sub-resolved") == 1000.0

    # 主 Agent 回答 → future 解除（从注册表移除）
    reg.set_answer("sub-resolved", "回答")
    assert reg.waiting_unique_names() == []

    db_monitor._remind_pending_asks(now=1030.0)
    assert len(pushed) == 1  # 不再推
    assert db_monitor._last_remind == {}  # 节流记录已清理


def test_remind_skipped_when_main_agent_busy(monkeypatch):
    """主 Agent 忙（_chat_lock.locked()）→ 不推提醒（消息等主 Agent 下一轮）。"""
    db_monitor._last_remind.clear()
    reg = PendingAskRegistry()
    reg.register("sub-busy")
    pushed = _setup(monkeypatch, registry=reg, chat_locked=True)

    db_monitor._remind_pending_asks(now=1000.0)

    assert pushed == []
    assert db_monitor._last_remind == {}


def test_remind_skipped_when_queue_not_empty(monkeypatch):
    """队列非空（ask 未投递仍留队列）→ 不推提醒（drain 先于 remind 时序保证）。"""
    db_monitor._last_remind.clear()
    reg = PendingAskRegistry()
    reg.register("sub-queued")
    pushed = _setup(monkeypatch, registry=reg, queue_empty=False)

    db_monitor._remind_pending_asks(now=1000.0)

    assert pushed == []
    assert db_monitor._last_remind == {}


def test_remind_no_waiting_no_push_and_clears_stale(monkeypatch):
    """无等待子 Agent → 不推 + 清理残留节流记录（旧 name 重新注册不误伤）。"""
    db_monitor._last_remind.clear()
    db_monitor._last_remind["ghost"] = 500.0
    pushed = _setup(monkeypatch, registry=PendingAskRegistry())

    db_monitor._remind_pending_asks(now=1000.0)

    assert pushed == []
    assert db_monitor._last_remind == {}


def test_remind_multiple_waiting_subagents_independent_throttle(monkeypatch):
    """多个等待子 Agent 各自收到提醒，节流按子 Agent 独立。"""
    db_monitor._last_remind.clear()
    reg = PendingAskRegistry()
    reg.register("sub-a")
    reg.register("sub-b")
    pushed = _setup(monkeypatch, registry=reg)

    db_monitor._remind_pending_asks(now=1000.0)
    assert len(pushed) == 2
    contents = [p[1] for p in pushed]
    assert any("@sub-a" in c for c in contents)
    assert any("@sub-b" in c for c in contents)

    # sub-a 解除（主 Agent 已回复）→ 清理其节流记录；sub-b 满 30s 仍收到提醒
    reg.set_answer("sub-a", "回答")
    db_monitor._remind_pending_asks(now=1030.0)
    assert len(pushed) == 3
    assert "@sub-b" in pushed[-1][1]
    assert "sub-a" not in db_monitor._last_remind


def test_remind_skipped_when_queue_snapshot_not_empty(monkeypatch):
    """迭代开始快照队列非空（ask 未投递仍留队列）→ 跳过（P2 队列守卫）。

    直接调用传 ask_queue_empty=False 模拟 run_db_monitor 迭代开始快照：drain 同迭代
    投递 ask 后队列已被清空，但守卫用迭代开始快照（非空）→ 跳过——主 Agent 刚收到 ask
    还在 SSE→session 延迟窗口，催答无意义。
    """
    db_monitor._last_remind.clear()
    reg = PendingAskRegistry()
    reg.register("sub-snapshot")
    pushed = _setup(monkeypatch, registry=reg)

    db_monitor._remind_pending_asks(now=1000.0, ask_queue_empty=False)

    assert pushed == []
    assert db_monitor._last_remind == {}


def test_remind_notify_failure_not_throttled(monkeypatch):
    """notify 返回 False（推送失败——loop 不可用/无订阅者）→ 节流不记录，下轮重试。

    P3-1 语义：与链路 A C1 惯例一致（失败不 pop/不占槽位）——失败不记 _last_remind，
    下个 sweep 周期不节流可重试；且失败有日志（此前失败不可见）。
    """
    db_monitor._last_remind.clear()
    reg = PendingAskRegistry()
    reg.register("sub-fail")
    pushed = _setup(monkeypatch, registry=reg, notify_result=False)

    db_monitor._remind_pending_asks(now=1000.0)

    assert len(pushed) == 1  # 尝试推送了一次
    assert db_monitor._last_remind.get("sub-fail") is None  # 失败不记节流

    # 1s 后重试不被节流阻挡（若失败也记了节流，1000~1030 内不会重试）
    db_monitor._remind_pending_asks(now=1001.0)
    assert len(pushed) == 2
    assert db_monitor._last_remind.get("sub-fail") is None  # 仍失败仍不记


class _StatefulQueue:
    """is_empty() 模拟迭代开始快照；drain() 由 drain stub 调用模拟链路 A pop 清空。

    用于验证 P2 接线：ask_queue_empty 快照在 drain **前**捕获——drain 同迭代清空队列后，
    remind 收到的仍是迭代开始（非空）快照（守卫跳过 → 不提前提醒）。
    """

    def __init__(self):
        self._empty = False  # 迭代开始时队列非空（ask 在队列未投递）

    def is_empty(self):
        return self._empty

    def drain(self):
        self._empty = True


async def test_run_db_monitor_sweep_wiring(monkeypatch):
    """run_db_monitor 主循环接线：首轮立即扫描 + 30s 周期 + drain 先于 remind + P2 快照。

    桩依赖（全部 monkeypatch 回滚）：_init_routed_baseline/_poll_messages/_drain_main_agent_request_queue/
    _remind_pending_asks/asyncio.sleep/time.time/队列单例。时间注入：时钟从 1000.0 起每迭代 +10s，
    sleep 第 4 次抛 CancelledError 结束循环（run_db_monitor 捕获 CancelledError 正常退出）。

    验证：
    - 首轮立即扫描：_last_remind_sweep 初始 0.0，迭代1（now=1000）即触发 sweep（1000-0≥30）
    - 30s 周期：1010/1020 不扫，迭代4（now=1030）再扫
    - 每迭代 drain 先于 remind（poll → drain → remind 顺序）
    - P2 快照：迭代1 drain 同迭代清空队列后，remind 收到的是迭代开始（非空）快照
      （ask_queue_empty=False → 守卫跳过）。drain 同迭代竞态无法在单元层精确复现
      （依赖主循环真实时序）——迭代开始快照在逻辑上消除该竞态，此处验证接线语义。
    """
    calls = []

    async def fake_init():
        calls.append("init")

    async def fake_poll():
        calls.append("poll")

    async def fake_drain():
        calls.append("drain")
        q.drain()  # 模拟链路 A：投递 ask 后清空队列

    def fake_remind(now=None, ask_queue_empty=None):
        calls.append(("remind", now, ask_queue_empty))

    clock = {"now": 1000.0}
    sleeps = {"n": 0}

    async def fake_sleep(_interval):
        sleeps["n"] += 1
        if sleeps["n"] >= 4:
            raise asyncio.CancelledError
        clock["now"] += 10.0

    q = _StatefulQueue()
    monkeypatch.setattr(db_monitor, "_init_routed_baseline", fake_init)
    monkeypatch.setattr(db_monitor, "_poll_messages", fake_poll)
    monkeypatch.setattr(db_monitor, "_drain_main_agent_request_queue", fake_drain)
    monkeypatch.setattr(db_monitor, "_remind_pending_asks", fake_remind)
    monkeypatch.setattr(db_monitor.time, "time", lambda: clock["now"])
    monkeypatch.setattr(db_monitor.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(db_monitor, "_last_remind_sweep", 0.0)
    monkeypatch.setattr(q_mod, "get_main_agent_request_queue", lambda: q)

    await db_monitor.run_db_monitor(interval=0.01)

    # 首轮立即扫描 + 30s 周期：迭代1（1000）与迭代4（1030）各一次，1010/1020 不扫
    reminds = [c for c in calls if c[0] == "remind"]
    assert [(now, empty) for _, now, empty in reminds] == [(1000.0, False), (1030.0, True)]
    # drain 先于 remind（首轮 poll → drain → remind 顺序）
    assert calls.index("drain") < calls.index(("remind", 1000.0, False))
    # P2：迭代1 drain 已清空队列，remind 仍收到迭代开始快照 False（ask 未投递 → 守卫跳过）
    assert reminds[0][2] is False
