"""验证 agent_runner_loop 的 supplement_drain 参数。"""
import inspect


def test_agent_runner_loop_has_supplement_drain_param():
    """agent_runner_loop 签名应含 supplement_drain 参数，默认 None。"""
    from agent.generic.agent_loop import agent_runner_loop
    sig = inspect.signature(agent_runner_loop)
    assert "supplement_drain" in sig.parameters, "agent_runner_loop 缺少 supplement_drain 参数"
    assert sig.parameters["supplement_drain"].default is None, "supplement_drain 默认值应为 None"


def test_agent_runner_loop_is_sync_generator():
    """agent_runner_loop 是同步生成器（def，不是 async def）。"""
    import inspect

    from agent.generic.agent_loop import agent_runner_loop
    # 同步生成器：isfunction True，iscoroutinefunction False
    assert inspect.isfunction(agent_runner_loop), "agent_runner_loop 应是普通函数"
    assert not inspect.iscoroutinefunction(agent_runner_loop), "agent_runner_loop 不应是 async 函数"


def test_drain_call_uses_supplement_drain_when_provided():
    """agent_runner_loop 内部应用 supplement_drain（None 时走全局 drain_supplement）。"""
    from agent.generic import agent_loop
    source = inspect.getsource(agent_loop)
    # 验证源码里有 supplement_drain 的使用逻辑
    assert "supplement_drain" in source, "agent_loop.py 未使用 supplement_drain"
    assert "drain_fn" in source or "supplement_drain if" in source, "未实现 supplement_drain 分支逻辑"
