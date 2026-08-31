"""ask_user 等待期间 chat() 生成器不结束 → cleanup 不触发 → 挂起子 Agent 保留。"""
import asyncio
import threading
import time

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
    # P3-4：轮询等 do_ask_user 完成 register（固定 sleep 有竞态——注入前未 register 时
    # set_answer 是 no-op，测试会静默挂死到 done.wait 超时）
    deadline = time.time() + 2
    while not registry.is_waiting("main-agent"):
        if time.time() > deadline:
            pytest.fail("do_ask_user 未在 2s 内注册 main-agent future")
        time.sleep(0.01)
    # 等待期间：未完成（工具循环还在阻塞）
    assert not done.is_set()
    # 注入回答
    registry.set_answer("main-agent", "继续")
    done.wait(timeout=2)
    assert results == ["[user 回答] 继续"]


def test_cleanup_unregisters_without_notify(monkeypatch):
    """cleanup 注销挂起同步子 Agent 时不推送 MainAgentRequestQueue 通知（2026-08-11 用户拍板）：
    工具错误/orphan 反馈已告知主 Agent，通知以 user 消息混入对话流会被误认为用户话。"""
    from agent.runner import cleanup_suspended_sync_subagents
    from agent.subagent_registry import SubagentRegistry

    # 用真实 register（内部新建 RunningSubagent，state 默认 running——需手动置 waiting_for_answer）
    SubagentRegistry.register(
        "nutritionist", object(), force_unique_name="nutritionist")
    inst = SubagentRegistry.get("nutritionist")
    inst.state = "waiting_for_answer"
    # 源模块 patch 记录 push 是否被调（cleanup 应完全不 push）
    pushed = []
    import agent.main_agent_request_queue as q_mod
    monkeypatch.setattr(q_mod, "get_main_agent_request_queue",
                        staticmethod(lambda: type("_Q", (), {"push": staticmethod(lambda c: pushed.append(c))})()))
    try:
        assert SubagentRegistry.get("nutritionist") is not None
        cleanup_suspended_sync_subagents({"result": "STOPPED"})
        assert SubagentRegistry.get("nutritionist") is None  # 已注销
        assert pushed == []  # 不推送清理通知（工具错误已反馈主 Agent）
    finally:
        SubagentRegistry.unregister("nutritionist")
