"""ask_user 等待期间 chat() 生成器不结束 → cleanup 不触发 → 挂起子 Agent 保留。"""
import asyncio
import threading
import pytest


def test_ask_user_blocking_keeps_generator_alive(monkeypatch):
    """do_ask_user 阻塞等待期间（future 未完成），调用方生成器不结束。"""
    from agent.ask_user import get_user_ask_registry
    from agent.handler import NiuHandler

    registry = get_user_ask_registry()
    h = NiuHandler.__new__(NiuHandler)
    # patch agent.ask_user（do_ask_user 函数体内 import，patch agent.handler 无效）
    monkeypatch.setattr("agent.ask_user.get_user_ask_registry", lambda: registry)
    monkeypatch.setattr("niu_api.chat._main_loop", type("L", (), {"is_closed": lambda self: False,
                                                                 "call_soon_threadsafe": lambda self, fn, e: fn(e)})())
    # R3-P1-1：推送闸门要求 _event_subscribers 非空（否则走"无法显示"早退）
    monkeypatch.setattr("niu_api.chat._event_subscribers", [asyncio.Queue()])

    results = []
    done = threading.Event()

    def _run():
        out = h.do_ask_user({"question": "继续？"}, None)
        results.append(out.data)
        done.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    import time
    time.sleep(0.1)
    # 等待期间：未完成（工具循环还在阻塞）
    assert not done.is_set()
    # 注入回答
    registry.set_answer("main-agent", "继续")
    done.wait(timeout=2)
    assert results == ["[user 回答] 继续"]


def test_cleanup_notifies_and_unregisters(monkeypatch):
    """cleanup 注销挂起同步子 Agent 时 MainAgentRequestQueue 通知（不静默）；注册表实例被清。"""
    from agent.runner import cleanup_suspended_sync_subagents
    from agent.subagent_registry import SubagentRegistry

    # 用真实 register（内部新建 RunningSubagent，state 默认 running——需手动置 waiting_for_answer）
    unique = SubagentRegistry.register(
        "nutritionist", object(), force_unique_name="nutritionist")
    inst = SubagentRegistry.get("nutritionist")
    inst.state = "waiting_for_answer"
    # R4-A/B P1：生产代码函数内 `from agent.main_agent_request_queue import get_main_agent_request_queue`
    # ——正确 patch 目标是**源模块**（patch agent.runner 同名属性无效——runner 无此模块属性，
    # monkeypatch 默认 raising=True 会抛 AttributeError）
    pushed = []
    import agent.main_agent_request_queue as q_mod
    monkeypatch.setattr(q_mod, "get_main_agent_request_queue",
                        staticmethod(lambda: type("_Q", (), {"push": staticmethod(lambda c: pushed.append(c))})()))
    try:
        assert SubagentRegistry.get("nutritionist") is not None
        cleanup_suspended_sync_subagents()
        assert SubagentRegistry.get("nutritionist") is None  # 已注销
        assert pushed and "已被轮末清理" in pushed[0]  # 通知主 Agent 不静默
    finally:
        SubagentRegistry.unregister("nutritionist")
