"""db_monitor 第三链路 _remind_pending_asks（等待状态机）单元测试。

确定性设计：直接调 _remind_pending_asks(now=...) 函数级断言——
now 参数注入代替真实时钟，每子 Agent 30s 节流可精确复现（不依赖 time.sleep/真实时钟）。

依赖全部 patch 在**源模块**上（_remind_pending_asks 函数内 from ... import 从源模块取属性）：
- agent.ask_main_agent.get_pending_ask_registry（注册表 → waiting_unique_names 数据源）
- niu_api.compat._chat_lock（主 Agent 忙判断）
- agent.main_agent_request_queue.get_main_agent_request_queue（队列非空守卫）
- niu_api.chat.notify_new_message_sync（推送通道断言）
"""
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


def _setup(monkeypatch, registry=None, chat_locked=False, queue_empty=True):
    """组装 _remind_pending_asks 的依赖并返回推送捕获列表。

    所有 patch 均为 monkeypatch 自动回滚，_last_remind 由各用例显式清理。
    """
    monkeypatch.setattr(ask_mod, "get_pending_ask_registry", lambda: registry)
    monkeypatch.setattr(compat_mod, "_chat_lock", _FakeLock(chat_locked))
    monkeypatch.setattr(q_mod, "get_main_agent_request_queue", lambda: _FakeQueue(queue_empty))
    pushed = []

    def fake_notify(_message_id, role, content, source="electron"):
        pushed.append((role, content, source))
        return True

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
