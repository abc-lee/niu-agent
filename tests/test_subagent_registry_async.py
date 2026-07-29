"""SubagentRegistry 异步子 Agent 扩展单元测试（阶段二 Task 3）。"""
import asyncio

from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_registry import SubagentRegistry
from agent.subagent_supplement import SubagentSupplementQueue


def setup_function():
    """每个测试前清空注册表。"""
    SubagentRegistry._instances.clear()


def test_register_async_subagent_with_task_and_memory_context():
    """异步子 Agent 注册时带 task 和 memory_context，is_sync=False。"""
    sq = SubagentSupplementQueue("test-async-0001")
    mc = SubagentMemoryContext()

    async def dummy_coro():
        await asyncio.sleep(0.01)

    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(dummy_coro())
        name = SubagentRegistry.register(
            "test-async",
            supplement_queue=sq,
            memory_context=mc,
            is_sync=False,
            task=task,
        )
        try:
            instance = SubagentRegistry.get(name)
            assert instance is not None
            assert instance.is_sync is False
            assert instance.task is task
            assert instance.memory_context is mc
            assert instance.started_at > 0  # started_at 应有值
        finally:
            SubagentRegistry.unregister(name)
            task.cancel()
            try:
                loop.run_until_complete(task)
            except asyncio.CancelledError:
                pass
    finally:
        loop.close()


def test_register_sync_subagent_task_is_none():
    """同步子 Agent 注册时不传 task，task 字段为 None。"""
    sq = SubagentSupplementQueue("test-sync-0001")
    name = SubagentRegistry.register("test-sync", supplement_queue=sq, is_sync=True)
    try:
        instance = SubagentRegistry.get(name)
        assert instance.is_sync is True
        assert instance.task is None
        assert instance.memory_context is None
        assert instance.started_at > 0
    finally:
        SubagentRegistry.unregister(name)


def test_list_running_filters_by_is_sync():
    """list_running 返回所有，调用方按 is_sync 过滤。"""
    sq1 = SubagentSupplementQueue("test-filter-0001")
    sq2 = SubagentSupplementQueue("test-filter-0002")
    n1 = SubagentRegistry.register("test-filter", supplement_queue=sq1, is_sync=True)
    n2 = SubagentRegistry.register(
        "test-filter",
        supplement_queue=sq2,
        is_sync=False,
        memory_context=SubagentMemoryContext(),
    )
    try:
        running = SubagentRegistry.list_running()
        sync = [r for r in running if r.is_sync]
        async_ = [r for r in running if not r.is_sync]
        assert any(r.unique_name == n1 for r in sync)
        assert any(r.unique_name == n2 for r in async_)
    finally:
        SubagentRegistry.unregister(n1)
        SubagentRegistry.unregister(n2)


def test_register_with_force_unique_name():
    """force_unique_name 透传：register 用指定名字而非随机 hex"""
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    name = SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq,
        force_unique_name="browser-operator",
    )
    assert name == "browser-operator"
    instance = SubagentRegistry.get(name)
    assert instance is not None
    assert instance.agent_type == "browser-operator"
    SubagentRegistry.unregister(name)


def test_register_force_unique_name_conflict():
    """force_unique_name 同名冲突 → 抛 ValueError（同步路径同类型只能跑一个）"""
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq1 = SubagentSupplementQueue(unique_name="")
    name1 = SubagentRegistry.register(
        agent_type="browser-operator",
        supplement_queue=sq1,
        force_unique_name="browser-operator",
    )
    assert name1 == "browser-operator"

    sq2 = SubagentSupplementQueue(unique_name="")
    try:
        SubagentRegistry.register(
            agent_type="browser-operator",
            supplement_queue=sq2,
            force_unique_name="browser-operator",
        )
        raise AssertionError("应抛 ValueError（同名冲突）")
    except ValueError as e:
        assert "browser-operator" in str(e)
        assert "已在运行" in str(e) or "已存在" in str(e)
    finally:
        SubagentRegistry.unregister(name1)


def test_register_without_force_unique_name_uses_random_hex():
    """不传 force_unique_name → 仍用随机 hex 后缀（异步路径保持原逻辑）"""
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue

    sq = SubagentSupplementQueue(unique_name="")
    name = SubagentRegistry.register(
        agent_type="file-processor",
        supplement_queue=sq,
        is_sync=False,
    )
    assert name.startswith("file-processor-")
    assert len(name) == len("file-processor-") + 4  # 4 位 hex 后缀
    SubagentRegistry.unregister(name)
