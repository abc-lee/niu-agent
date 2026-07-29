"""SubagentRegistry 单元测试（阶段一简化版）。"""
import threading
from unittest.mock import MagicMock

from agent.subagent_registry import RunningSubagent, SubagentRegistry


def setup_function():
    """每个测试前清空注册表。"""
    SubagentRegistry._instances.clear()


def test_register_returns_unique_name():
    q = MagicMock()
    name = SubagentRegistry.register(agent_type="file-processor", supplement_queue=q)
    assert name.startswith("file-processor-")
    assert len(name) == len("file-processor-") + 4  # 4 位 hex 后缀


def test_register_no_collision():
    """注册多次同类型，名字不碰撞。"""
    names = set()
    for _ in range(100):
        name = SubagentRegistry.register("file-processor", MagicMock())
        assert name not in names
        names.add(name)


def test_unregister():
    q = MagicMock()
    name = SubagentRegistry.register("file-processor", q)
    assert SubagentRegistry.get(name) is not None
    SubagentRegistry.unregister(name)
    assert SubagentRegistry.get(name) is None


def test_list_running():
    q1 = MagicMock()
    q2 = MagicMock()
    n1 = SubagentRegistry.register("file-processor", q1)
    n2 = SubagentRegistry.register("context-manager", q2)
    running = SubagentRegistry.list_running()
    names = [r.unique_name for r in running]
    assert n1 in names
    assert n2 in names
    assert len(running) == 2


def test_list_running_returns_copy():
    """list_running 返回副本，外部修改不影响内部。"""
    SubagentRegistry.register("file-processor", MagicMock())
    running = SubagentRegistry.list_running()
    running.clear()
    assert len(SubagentRegistry.list_running()) == 1


def test_get_returns_running_subagent():
    q = MagicMock()
    name = SubagentRegistry.register("file-processor", q)
    inst = SubagentRegistry.get(name)
    assert isinstance(inst, RunningSubagent)
    assert inst.unique_name == name
    assert inst.agent_type == "file-processor"
    assert inst.supplement_queue is q
    assert inst.memory_context is None  # 阶段一同步子 Agent 无 memory_context


def test_concurrent_register():
    """多线程同时 register，无碰撞无数据竞争。"""
    names = set()
    names_lock = threading.Lock()

    def worker():
        for _ in range(20):
            name = SubagentRegistry.register("file-processor", MagicMock())
            with names_lock:
                assert name not in names
                names.add(name)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(names) == 100


def test_running_subagent_default_fields():
    """RunningSubagent 新增 6 字段默认值正确"""
    from agent.subagent_supplement import SubagentSupplementQueue
    sq = SubagentSupplementQueue(unique_name="")
    r = RunningSubagent(unique_name="test-ab12", agent_type="test", supplement_queue=sq)
    assert r.state == "running"
    assert r.suspended_messages is None
    assert r.suspended_handler is None
    assert r.suspended_client is None
    assert r.suspended_tools_schema is None
    assert r.suspended_system_message is None


def test_running_subagent_state_transition():
    """state 字段可被外部修改"""
    from agent.subagent_supplement import SubagentSupplementQueue
    sq = SubagentSupplementQueue(unique_name="")
    r = RunningSubagent(unique_name="test-ab12", agent_type="test", supplement_queue=sq)
    r.state = "waiting_for_answer"
    assert r.state == "waiting_for_answer"
    r.state = "running"
    assert r.state == "running"
