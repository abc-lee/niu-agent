"""验证子 Agent 用独立 supplement queue + 不检查全局 stop 信号灯。"""
import inspect


def test_call_subagent_accepts_supplement_queue():
    """call_subagent 签名应接受 supplement_queue 参数。"""
    from agent.subagent import call_subagent
    sig = inspect.signature(call_subagent)
    assert "supplement_queue" in sig.parameters, "call_subagent 缺少 supplement_queue 参数"


def test_run_agent_loop_no_is_stop_requested():
    """_run_agent_loop 不应再调 is_stop_requested。"""
    from agent.subagent import _run_agent_loop
    source = inspect.getsource(_run_agent_loop)
    assert "is_stop_requested" not in source, "_run_agent_loop 仍在检查 is_stop_requested"


def test_run_agent_loop_enable_supplement_true():
    """_run_agent_loop 调 agent_runner_loop 时 enable_supplement 应为 True，且传 supplement_drain。"""
    from agent.subagent import _run_agent_loop
    source = inspect.getsource(_run_agent_loop)
    assert "enable_supplement=False" not in source, "_run_agent_loop 仍硬编码 enable_supplement=False"
    assert "enable_supplement=True" in source or "supplement_drain=" in source, "_run_agent_loop 未启用 supplement"


def test_call_subagent_registers_to_registry():
    """call_subagent 应注册到 SubagentRegistry。"""
    from agent.subagent import call_subagent
    source = inspect.getsource(call_subagent)
    assert "SubagentRegistry" in source, "call_subagent 未使用 SubagentRegistry"
    assert "register" in source, "call_subagent 未注册到 Registry"
    assert "unregister" in source, "call_subagent 未从 Registry 注销"
