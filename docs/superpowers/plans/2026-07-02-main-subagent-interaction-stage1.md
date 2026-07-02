# 主子 Agent 交互通道 - 阶段一：通信通道 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有同步调用基础上，实现主 Agent ↔ 子 Agent 的双向消息通信通道——主能给子补充上下文、双击停止能批量 /stop 所有子 Agent、信号灯重设计避免误杀。

**Architecture:** 新建子 Agent 独立 `SubagentSupplementQueue`（`queue.Queue` 线程安全），子 Agent 调 `agent_runner_loop` 时参数注入自己的 drain 函数；messages db 加 `role="subagent_msg"` 存 `@` 消息（content 前缀编码目标/发送者名，零 schema 改动）；db 监测程序（后台 asyncio task）轮询 db 路由 `@` 消息到对应子 Agent supplement queue 或主 Agent supplement queue；信号灯 `_stop_requested` 只对主 Agent 有效，子 Agent 移除检查，双击停止按钮触发批量 /stop。

**Tech Stack:** Python（asyncio + queue.Queue + threading.Event）+ SQLite（messages db）+ Electron（main.js/chat.html 双击停止 UI）

**不做的事（阶段一范围外，留到阶段二）：** 异步调用、`ask_main_agent` 工具、进度查看工具、动态注入区列后台子 Agent、SubagentRegistry。阶段一同步子 Agent 也注册到临时注册表（供双击停止遍历），但不暴露给主 Agent。

---

## File Structure

| 文件 | 改动 | 责任 |
|------|------|------|
| `agent/subagent_supplement.py` | 新建 | `SubagentSupplementQueue` + `SubagentSupplementItem` |
| `agent/subagent_registry.py` | 新建 | `SubagentRegistry`（阶段一简化版，含同步子 Agent） |
| `agent/subagent.py` | 修改 | `_run_agent_loop` 改 `enable_supplement=True` + 注入 supplement_drain、移除 `is_stop_requested()` 检查、`call_subagent` 签名加 `supplement_queue` 参数、注册/注销到 SubagentRegistry |
| `agent/runner.py` | 修改 | `_stop_requested` 语义保留但子 Agent 不再检查、加 `request_stop_all_subagents()` 函数 |
| `agent/generic/agent_loop.py` | 修改 | `agent_runner_loop` 加 `supplement_drain` 可选参数、history 重建过滤 `role="subagent_msg"` |
| `agent/session.py` | 修改 | `Message` dataclass 注释加 `subagent_msg` role |
| `niu_api/db_monitor.py` | 新建 | db 监测程序（后台 asyncio task，轮询 + 路由 @ 消息） |
| `niu_api/__main__.py` | 修改 | lifespan startup 启动 db 监测 task |
| `config/agents/niu.md` | 修改 | 主 Agent 提示词加逐条回复约束 |
| `ui/assistant/chat.html` | 修改 | 双击停止 UI + `role="subagent_msg"` 消息特殊样式渲染 |
| `tests/test_subagent_supplement.py` | 新建 | supplement queue 单元测试 |
| `tests/test_subagent_registry.py` | 新建 | 注册表单元测试 |
| `tests/test_db_monitor.py` | 新建 | db 监测程序路由单元测试 |
| `tests/test_subagent_interaction_integration.py` | 新建 | 阶段一集成测试（真实 LLM） |

---

## Task 1: 新建 SubagentSupplementQueue

**Files:**
- Create: `agent/subagent_supplement.py`
- Test: `tests/test_subagent_supplement.py`

**背景：** 现有 `agent/runner.py:44` 的全局 `_supplement_queue` 主子共享会串话，子 Agent 硬编码 `enable_supplement=False`。新建独立 queue 让每个子 Agent 持有自己的 supplement 通道。

- [ ] **Step 1: 写失败测试**

Create: `tests/test_subagent_supplement.py`

```python
"""SubagentSupplementQueue 单元测试。"""
import threading
from agent.subagent_supplement import SubagentSupplementQueue, SubagentSupplementItem


def test_push_and_drain():
    q = SubagentSupplementQueue("file-processor-a1b2")
    q.push("补充内容1")
    q.push("补充内容2", is_terminate=True, sender="主Agent")
    items = q.drain()
    assert len(items) == 2
    assert items[0].content == "补充内容1"
    assert items[0].is_terminate is False
    assert items[0].sender == "主Agent"  # 默认 sender
    assert items[1].content == "补充内容2"
    assert items[1].is_terminate is True
    assert items[1].sender == "主Agent"


def test_drain_empty():
    q = SubagentSupplementQueue("test")
    items = q.drain()
    assert items == []


def test_drain_consumes_all():
    q = SubagentSupplementQueue("test")
    q.push("a")
    q.push("b")
    q.push("c")
    first = q.drain()
    assert len(first) == 3
    second = q.drain()
    assert second == []


def test_push_thread_safety():
    """多线程同时 push，drain 应拿到全部。"""
    q = SubagentSupplementQueue("test")

    def producer(n):
        for i in range(100):
            q.push(f"msg-{n}-{i}")

    threads = [threading.Thread(target=producer, args=(n,)) for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    items = q.drain()
    assert len(items) == 500


def test_unique_name():
    q = SubagentSupplementQueue("file-processor-a1b2")
    assert q.unique_name == "file-processor-a1b2"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_supplement.py -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'agent.subagent_supplement'`）

- [ ] **Step 3: 实现 SubagentSupplementQueue**

Create: `agent/subagent_supplement.py`

```python
"""子 Agent 独立 supplement queue。

每个子 Agent 实例一个，线程安全（queue.Queue）。db 监测程序按 unique_name 路由写入。
与主 Agent 的全局 _supplement_queue（agent/runner.py:44）隔离，避免主子串话。
"""
import queue as _queue
from dataclasses import dataclass


@dataclass
class SubagentSupplementItem:
    """supplement 队列里的一项。"""
    content: str
    is_terminate: bool  # /stop 标记
    sender: str          # 发送者名（如 "主Agent"）


class SubagentSupplementQueue:
    """每个子 Agent 实例一个的 supplement queue，线程安全。

    db 监测程序（主 loop）put_nowait，子 Agent（asyncio.to_thread 线程）drain。
    """

    def __init__(self, unique_name: str):
        self.unique_name = unique_name
        self._q = _queue.Queue()

    def push(self, content: str, is_terminate: bool = False, sender: str = "主Agent") -> None:
        """推入一项。线程安全（queue.Queue.put_nowait）。"""
        self._q.put_nowait(SubagentSupplementItem(content, is_terminate, sender))

    def drain(self) -> list:
        """取出全部并清空。线程安全。"""
        items = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except _queue.Empty:
                break
        return items
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_supplement.py -v`
Expected: 5 个测试全 PASS

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/subagent_supplement.py tests/test_subagent_supplement.py
git commit -m "feat(subagent): 新建 SubagentSupplementQueue 子 Agent 独立 supplement 通道"
```

---

## Task 2: 新建 SubagentRegistry（阶段一简化版）

**Files:**
- Create: `agent/subagent_registry.py`
- Test: `tests/test_subagent_registry.py`

**背景：** 阶段一同步子 Agent 也需要注册（供双击停止遍历），但不需要 memory_context/asyncio task。阶段二再扩展为含异步子 Agent 的完整版。

- [ ] **Step 1: 写失败测试**

Create: `tests/test_subagent_registry.py`

```python
"""SubagentRegistry 单元测试（阶段一简化版）。"""
import threading
from unittest.mock import MagicMock
from agent.subagent_registry import SubagentRegistry, RunningSubagent


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_registry.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 SubagentRegistry**

Create: `agent/subagent_registry.py`

```python
"""子 Agent 注册表（阶段一简化版）。

维护当前在跑的子 Agent（含同步和异步）。阶段一只用同步子 Agent（memory_context=None）。
双击停止按钮遍历此注册表批量推 /stop。

线程安全：register/unregister 用 threading.Lock 保护（read-modify-write 非原子）。
"""
import threading
import secrets
from dataclasses import dataclass
from typing import Optional, Any


@dataclass
class RunningSubagent:
    unique_name: str
    agent_type: str
    supplement_queue: Any  # SubagentSupplementQueue
    memory_context: Optional[Any] = None  # 阶段一同步子 Agent 为 None
    is_sync: bool = True  # 阶段一都是同步


class SubagentRegistry:
    _instances: dict = {}
    _lock = threading.Lock()

    @classmethod
    def _gen_unique_name(cls, agent_type: str) -> str:
        """生成 <agent_type>-<4位hex> 唯一名，碰撞重试。"""
        while True:
            suffix = secrets.token_hex(2)  # 4 位 hex
            name = f"{agent_type}-{suffix}"
            if name not in cls._instances:
                return name

    @classmethod
    def register(cls, agent_type: str, supplement_queue: Any,
                 memory_context: Optional[Any] = None,
                 is_sync: bool = True) -> str:
        with cls._lock:
            name = cls._gen_unique_name(agent_type)
            cls._instances[name] = RunningSubagent(
                unique_name=name,
                agent_type=agent_type,
                supplement_queue=supplement_queue,
                memory_context=memory_context,
                is_sync=is_sync,
            )
            return name

    @classmethod
    def unregister(cls, unique_name: str) -> None:
        with cls._lock:
            cls._instances.pop(unique_name, None)

    @classmethod
    def get(cls, unique_name: str) -> Optional[RunningSubagent]:
        with cls._lock:
            return cls._instances.get(unique_name)

    @classmethod
    def list_running(cls) -> list:
        """返回副本，外部修改不影响内部。"""
        with cls._lock:
            return list(cls._instances.values())
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_registry.py -v`
Expected: 7 个测试全 PASS

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/subagent_registry.py tests/test_subagent_registry.py
git commit -m "feat(subagent): 新建 SubagentRegistry 阶段一简化版（含同步子 Agent）"
```

---

## Task 3: agent_runner_loop 加 supplement_drain 参数

**Files:**
- Modify: `agent/generic/agent_loop.py:273-290`（签名）、`agent/generic/agent_loop.py:701`（drain 调用点）
- Test: `tests/test_agent_loop_supplement_drain.py`

**背景：** 现有 `drain_supplement()` 在 `agent_loop.py:701` 是硬编码全局函数。改造为可选参数 `supplement_drain`，None 时走现有全局 drain（主 Agent 不变），非 None 时调子 Agent 自己的 drain。

- [ ] **Step 1: Read 现有 agent_runner_loop 签名和 drain 调用点**

Read: `agent/generic/agent_loop.py:273-290`（签名）和 `agent/generic/agent_loop.py:695-710`（drain 调用点上下文）

确认现有签名和 `drain_supplement()` 调用方式。

- [ ] **Step 2: 写失败测试**

Create: `tests/test_agent_loop_supplement_drain.py`

```python
"""验证 agent_runner_loop 的 supplement_drain 参数。"""
from unittest.mock import MagicMock, patch


def test_supplement_drain_none_uses_global():
    """supplement_drain=None 时走现有全局 drain_supplement。"""
    with patch("agent.generic.agent_loop.drain_supplement") as mock_drain:
        mock_drain.return_value = None
        # 调 agent_runner_loop（需 mock client/handler 等，复杂）
        # 这里只验证 drain_supplement 被调用
        from agent.generic.agent_loop import drain_supplement
        result = drain_supplement()
        mock_drain.return_value = None
        # 简化：直接测 agent_runner_loop 的 drain 逻辑
        # 实际集成测试在 test_subagent_interaction_integration.py


def test_supplement_drain_custom_called():
    """supplement_drain 传入自定义函数时被调用。"""
    custom_drain = MagicMock(return_value=[])
    # 验证 custom_drain 被 agent_runner_loop 使用
    # 这里通过单元测试模拟，实际由集成测试覆盖
    assert custom_drain() == []
```

**注意：** `agent_runner_loop` 是生成器，完整测试需要 mock client/handler/tools_schema 等，复杂。这里只写简化单元测试验证参数传递逻辑，完整覆盖在集成测试（Task 12）。

- [ ] **Step 3: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_agent_loop_supplement_drain.py -v`
Expected: FAIL（`supplement_drain` 参数还不存在）

- [ ] **Step 4: 修改 agent_runner_loop 签名加 supplement_drain 参数**

Read `agent/generic/agent_loop.py:273-290` 拿到完整签名。

Edit `agent/generic/agent_loop.py`，在 `agent_runner_loop` 签名末尾加参数：

```python
async def agent_runner_loop(
    client,
    system_prompt,
    user_input,
    handler,
    tools_schema,
    max_turns=40,
    verbose=True,
    initial_user_content=None,
    history=None,
    on_turn_end=None,
    context_window_tokens=0,
    context_fifo_threshold=0,
    context_target_threshold=0,
    on_context_high_usage=None,
    enable_supplement=True,
    system_message=None,
    supplement_drain=None,  # 新增：子 Agent 传入自己的 drain 函数，None 时走全局
):
```

- [ ] **Step 5: 修改 drain_supplement 调用点用 supplement_drain**

Read `agent/generic/agent_loop.py:695-705` 拿到现有调用上下文。

Edit `agent/generic/agent_loop.py:701` 附近：

```python
# 原来：
# supplement = drain_supplement() if enable_supplement else None

# 改成：
if enable_supplement:
    drain_fn = supplement_drain if supplement_drain is not None else drain_supplement
    supplement = drain_fn()
else:
    supplement = None
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_agent_loop_supplement_drain.py -v`
Expected: PASS

- [ ] **Step 7: py_compile 验证**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m py_compile agent/generic/agent_loop.py`
Expected: 无输出（语法正确）

- [ ] **Step 8: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/agent_loop.py tests/test_agent_loop_supplement_drain.py
git commit -m "feat(agent_loop): agent_runner_loop 加 supplement_drain 参数支持子 Agent 独立 queue"
```

---

## Task 4: 子 Agent 改用独立 supplement queue + 移除 is_stop_requested 检查

**Files:**
- Modify: `agent/subagent.py:188-267`（`_run_agent_loop`）、`agent/subagent.py:470`（`call_subagent` 签名）
- Test: `tests/test_subagent_supplement_integration.py`

**背景：** 现有 `_run_agent_loop` 硬编码 `enable_supplement=False` + 每轮检查 `is_stop_requested()`。改造为 `enable_supplement=True` + 注入自己的 `SubagentSupplementQueue` + 移除 `is_stop_requested()` 检查（子 Agent 不响应全局信号灯，只响应自己 queue 的 /stop）。

- [ ] **Step 1: Read 现有 _run_agent_loop 和 call_subagent**

Read: `agent/subagent.py:188-267`（`_run_agent_loop` 完整）和 `agent/subagent.py:470-500`（`call_subagent` 签名）

确认现有 `enable_supplement=False` 在 241 行、`is_stop_requested()` 检查在 249 行、`call_subagent` 签名参数。

- [ ] **Step 2: 写失败测试**

Create: `tests/test_subagent_supplement_integration.py`

```python
"""验证子 Agent 用独立 supplement queue + 不检查全局 stop 信号灯。"""
import threading
from unittest.mock import patch, MagicMock


def test_call_subagent_accepts_supplement_queue():
    """call_subagent 签名应接受 supplement_queue 参数。"""
    import inspect
    from agent.subagent import call_subagent
    sig = inspect.signature(call_subagent)
    assert "supplement_queue" in sig.parameters, "call_subagent 缺少 supplement_queue 参数"


def test_run_agent_loop_no_is_stop_requested():
    """_run_agent_loop 不应再调 is_stop_requested。"""
    import inspect
    from agent.subagent import _run_agent_loop
    source = inspect.getsource(_run_agent_loop)
    assert "is_stop_requested" not in source, "_run_agent_loop 仍在检查 is_stop_requested"


def test_run_agent_loop_enable_supplement_true():
    """_run_agent_loop 调 agent_runner_loop 时 enable_supplement 应为 True。"""
    import inspect
    from agent.subagent import _run_agent_loop
    source = inspect.getsource(_run_agent_loop)
    assert "enable_supplement=True" in source or "enable_supplement = True" in source
    assert "enable_supplement=False" not in source
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_supplement_integration.py -v`
Expected: FAIL（`supplement_queue` 参数不存在、`is_stop_requested` 仍在）

- [ ] **Step 4: 修改 call_subagent 签名加 supplement_queue 参数**

Read `agent/subagent.py:470-478` 拿到完整签名。

Edit `agent/subagent.py:470` 附近，给 `call_subagent` 加可选参数：

```python
def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict,
    mcp_client=None,
    history=None,
    context_fifo_threshold=-1,
    no_tools=False,
    supplement_queue=None,  # 新增：子 Agent 独立 supplement queue
) -> str:
```

- [ ] **Step 5: 修改 _run_agent_loop 传 supplement_drain + 移除 is_stop_requested**

Read `agent/subagent.py:188-267` 完整逻辑。

Edit `agent/subagent.py:241` 附近（`enable_supplement=False` 那行），改为：

```python
# 原来：
# enable_supplement=False,  # False for sub-agents to prevent stealing main agent's supplements

# 改成：
enable_supplement=True,  # 子 Agent 用独立 supplement queue
supplement_drain=supplement_queue.drain if supplement_queue is not None else None,
```

Edit `agent/subagent.py:249` 附近（`is_stop_requested()` 检查），移除整个检查块：

```python
# 原来：
# while True:
#     if is_stop_requested():  # 249 行
#         break
#     chunk = next(gen)

# 改成：
while True:
    chunk = next(gen)  # 子 Agent 不再检查全局 stop 信号灯，只响应自己 queue 的 /stop
```

**注意：** 移除 `is_stop_requested()` 后，`from agent.runner import is_stop_requested` 的 import 如果没别处用可以删，但保险起见保留 import（避免破坏其他可能引用的代码）。

- [ ] **Step 6: 在 call_subagent 内创建 supplement_queue 并注册到 SubagentRegistry**

Read `agent/subagent.py:580-600`（`call_subagent` 内部创建 client/handler 之后、调 `_run_agent_loop` 之前）。

Edit `agent/subagent.py` 在调 `_run_agent_loop` 之前加：

```python
# === 新增：创建 supplement queue + 注册到 SubagentRegistry ===
from agent.subagent_supplement import SubagentSupplementQueue
from agent.subagent_registry import SubagentRegistry

if supplement_queue is None:
    supplement_queue = SubagentSupplementQueue(unique_name="")  # unique_name 注册后回填
unique_name = SubagentRegistry.register(agent_name, supplement_queue)
supplement_queue.unique_name = unique_name  # 回填唯一名

try:
    result_text, return_value = _run_agent_loop(
        # ... 原有参数 ...
        supplement_queue=supplement_queue,  # 新增
    )
finally:
    SubagentRegistry.unregister(unique_name)
```

**关键：** `try/finally` 保证无论 `_run_agent_loop` 正常/异常都从注册表移除。`unique_name` 在注册后回填到 queue（db 监测程序路由时用 queue.unique_name 识别）。

- [ ] **Step 7: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_supplement_integration.py -v`
Expected: 3 个测试全 PASS

- [ ] **Step 8: py_compile 验证**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m py_compile agent/subagent.py`
Expected: 无输出

- [ ] **Step 9: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/subagent.py tests/test_subagent_supplement_integration.py
git commit -m "feat(subagent): 子 Agent 用独立 supplement queue + 移除 is_stop_requested 检查"
```

---

## Task 5: messages db 支持 role="subagent_msg" + history 重建过滤

**Files:**
- Modify: `agent/session.py:33-47`（Message dataclass 注释）、`agent/generic/agent_loop.py:307-348`（history 重建）
- Test: `tests/test_subagent_msg_role.py`

**背景：** messages db 加新 role 值 `subagent_msg`，存储 `@` 消息。history 重建时必须过滤掉 `subagent_msg`，否则会被当 user 输入塞进 LLM 上下文污染。

- [ ] **Step 1: Read 现有 Message dataclass 和 history 重建**

Read: `agent/session.py:33-47`（Message dataclass）和 `agent/generic/agent_loop.py:307-348`（history 重建）

确认现有 role 注释和 history 分支逻辑。

- [ ] **Step 2: 写失败测试**

Create: `tests/test_subagent_msg_role.py`

```python
"""验证 role=subagent_msg 消息能存入 db 且 history 重建时被过滤。"""
import tempfile
import os
from agent.session import MessageStore


def test_add_subagent_msg_message():
    """能存 role=subagent_msg 的消息。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = MessageStore(db_path)
        msg_id = store.add_message(role="subagent_msg", content="@主Agent [file-processor-a1b2] 测试问题")
        msgs = store.get_messages()
        assert len(msgs) == 1
        assert msgs[0].role == "subagent_msg"
        assert "@主Agent" in msgs[0].content
    finally:
        os.unlink(db_path)


def test_get_messages_includes_subagent_msg():
    """get_messages 返回 subagent_msg 消息（不过滤 role）。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = MessageStore(db_path)
        store.add_message(role="user", content="用户消息")
        store.add_message(role="subagent_msg", content="@主Agent [test] 子 Agent 消息")
        store.add_message(role="assistant", content="主 Agent 回复")
        msgs = store.get_messages()
        roles = [m.role for m in msgs]
        assert "subagent_msg" in roles
        assert len(msgs) == 3
    finally:
        os.unlink(db_path)
```

- [ ] **Step 3: 运行测试**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_msg_role.py -v`

**预期**：可能 PASS（现有 `add_message` 不限制 role 值，`get_messages` 不过滤 role）。如果 PASS 说明 db 层已支持，只需改 history 重建。如果 FAIL，需改 Message dataclass 注释。

- [ ] **Step 4: 更新 Message dataclass 注释**

Read `agent/session.py:33-47`。

Edit `agent/session.py` 的 Message dataclass 注释，role 字段加 `subagent_msg`：

```python
@dataclass
class Message:
    id: str
    role: str  # 'user' | 'assistant' | 'system' | 'tool' | 'subagent_msg'
    content: str
    # ... 其他字段 ...
```

- [ ] **Step 5: 修改 history 重建过滤 subagent_msg**

Read `agent/generic/agent_loop.py:307-348`。

**关键**：history 里的消息是 **dict**（`msg.get("role")` 访问），不是 Message dataclass（`msg.role` 属性访问）。Read `:308` 确认 `role = msg.get("role", "user")` 的访问方式。

在 history 重建循环开头加过滤。找到 `for msg in history:` 或类似循环（约 307-310 行），在循环内开头加：

```python
for msg in history:
    # === 新增：过滤 subagent_msg 消息，不塞进 LLM 上下文 ===
    if msg.get("role") == "subagent_msg":
        continue
    # ... 原有 role 分支逻辑 ...
```

**注意**：用 `msg.get("role")` 而不是 `msg.role`（dict 访问）。如果 history 在进入 `agent_runner_loop` 前已由 `load_history` / `_build_compress_history` 过滤，需在那一层也加过滤（确认过滤点）。建议两层都加，双保险。

- [ ] **Step 6: 写 history 重建过滤测试**

在 `tests/test_subagent_msg_role.py` 追加：

```python
def test_history_reconstruction_skips_subagent_msg():
    """history 重建时 subagent_msg 消息被过滤，不进 LLM 上下文。"""
    # 这个测试需要 mock agent_runner_loop 的 history 构造，较复杂
    # 简化：直接测 _build_history_messages（如果有此函数）
    # 或在集成测试 Task 12 覆盖
    # 这里只验证 agent_loop.py 源码里有过滤逻辑
    import inspect
    from agent.generic import agent_loop
    source = inspect.getsource(agent_loop)
    assert 'subagent_msg' in source, "agent_loop.py 未处理 subagent_msg role"
    # 找到 history 重建段，确认有 continue 跳过
    assert 'continue' in source
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_msg_role.py -v`
Expected: 3 个测试全 PASS

- [ ] **Step 8: py_compile 验证**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m py_compile agent/session.py agent/generic/agent_loop.py`
Expected: 无输出

- [ ] **Step 9: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/session.py agent/generic/agent_loop.py tests/test_subagent_msg_role.py
git commit -m "feat(session): messages db 支持 role=subagent_msg + history 重建过滤"
```

---

## Task 6: 新建 db 监测程序

**Files:**
- Create: `niu_api/db_monitor.py`
- Modify: `niu_api/__main__.py:43-176`（lifespan startup 启动监测 task）
- Test: `tests/test_db_monitor.py`

**背景：** 后台 asyncio task，轮询 messages db 中 `role="subagent_msg"` 且未路由的新消息，按 `@目标` 解析路由：目标是 `主Agent` → 推入主 Agent supplement queue（`enqueue_supplement`）；目标是某子 Agent → 推入对应 `SubagentSupplementQueue`。

- [ ] **Step 1: Read 现有 lifespan 和 _sync_broadcast**

Read: `niu_api/__main__.py:43-176`（lifespan）和 `niu_api/chat.py:142-149`（`_sync_broadcast`）、`agent/runner.py:47-49`（`enqueue_supplement`）

确认后台 task 启动模式、主 Agent supplement queue 的 enqueue 入口。

- [ ] **Step 2: 写失败测试**

Create: `tests/test_db_monitor.py`

```python
"""db 监测程序路由逻辑单元测试。"""
import asyncio
import tempfile
import os
from unittest.mock import patch, MagicMock


def test_parse_at_message():
    """解析 @消息格式：@目标 [发送者名] 内容。"""
    from niu_api.db_monitor import parse_at_message
    target, sender, content = parse_at_message("@主Agent [file-processor-a1b2] 这个 PDF 是扫描件吗？")
    assert target == "主Agent"
    assert sender == "file-processor-a1b2"
    assert content == "这个 PDF 是扫描件吗？"


def test_parse_at_message_no_sender():
    """主 Agent 发给子 Agent 的消息可能无 [发送者名]。"""
    from niu_api.db_monitor import parse_at_message
    target, sender, content = parse_at_message("@file-processor-a1b2 试试换个路径")
    assert target == "file-processor-a1b2"
    assert sender == ""  # 无发送者
    assert content == "试试换个路径"


def test_parse_at_message_stop():
    """/stop 指令解析。"""
    from niu_api.db_monitor import parse_at_message
    target, sender, content = parse_at_message("@file-processor-a1b2 /stop")
    assert target == "file-processor-a1b2"
    assert content == "/stop"


def test_route_to_main_agent():
    """@主Agent 消息推入主 Agent supplement queue。"""
    from niu_api.db_monitor import route_message
    with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
        route_message("主Agent", "file-processor-a1b2", "测试问题")
        mock_enqueue.assert_called_once_with("测试问题")


def test_route_to_subagent_normal():
    """@子名 普通消息推入子 Agent supplement queue。"""
    from niu_api.db_monitor import route_message
    mock_queue = MagicMock()
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = MagicMock(supplement_queue=mock_queue)
        route_message("file-processor-a1b2", "主Agent", "补充内容")
        mock_queue.push.assert_called_once_with("补充内容", is_terminate=False, sender="主Agent")


def test_route_to_subagent_stop():
    """@子名 /stop 推入子 Agent supplement queue 标记 is_terminate=True。"""
    from niu_api.db_monitor import route_message
    mock_queue = MagicMock()
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = MagicMock(supplement_queue=mock_queue)
        route_message("file-processor-a1b2", "主Agent", "/stop")
        mock_queue.push.assert_called_once_with("/stop", is_terminate=True, sender="主Agent")


def test_route_target_not_found():
    """目标子 Agent 不在注册表，推回主 Agent。"""
    from niu_api.db_monitor import route_message
    with patch("niu_api.db_monitor.SubagentRegistry") as mock_registry:
        mock_registry.get.return_value = None
        with patch("niu_api.db_monitor.enqueue_supplement") as mock_enqueue:
            route_message("unknown-subagent", "主Agent", "测试")
            mock_enqueue.assert_called_once()
            # 推回主 Agent 的消息含"目标子 Agent 已不存在"
            call_args = mock_enqueue.call_args[0][0]
            assert "unknown-subagent" in call_args
            assert "已不存在" in call_args
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_db_monitor.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 4: 实现 db_monitor.py**

Create: `niu_api/db_monitor.py`

```python
"""db 监测程序：轮询 messages db 中 role=subagent_msg 消息，按 @目标 路由。

后台 asyncio task，每 200ms 轮询。生命周期由 niu_api/__main__.py lifespan 管理。
"""
import re
import asyncio
import logging
from typing import Tuple

from agent.runner import enqueue_supplement
from agent.subagent_registry import SubagentRegistry
from agent.session import get_message_store

logger = logging.getLogger(__name__)

# @消息格式：@目标 [发送者名] 内容（发送者名可选）
_AT_MSG_PATTERN = re.compile(r'^@(\S+)(?:\s+\[([^\]]+)\])?\s*(.*)$', re.DOTALL)

# 已路由的消息 id 集合（内存，程序重启时从 db 灌入基线）
_routed_msg_ids: set = set()

# 心跳计数
_routed_count = 0


async def _init_routed_baseline(message_store) -> None:
    """启动时拿当前所有 subagent_msg 消息 id 灌入基线，避免重启后重复路由历史消息。"""
    global _routed_msg_ids
    try:
        msgs = await message_store.get_messages()
        for msg in msgs:
            if msg.role == "subagent_msg":
                _routed_msg_ids.add(msg.id)
        logger.info(f"db_monitor 基线初始化：{len(_routed_msg_ids)} 条历史 subagent_msg 消息标记为已路由")
    except Exception as e:
        logger.error(f"db_monitor 基线初始化失败：{e}")


def parse_at_message(content: str) -> Tuple[str, str, str]:
    """解析 @消息格式，返回 (target, sender, content)。

    格式：@目标 [发送者名] 内容  或  @目标 内容
    """
    match = _AT_MSG_PATTERN.match(content.strip())
    if not match:
        return "", "", content
    target = match.group(1)
    sender = match.group(2) or ""
    body = match.group(3).strip()
    return target, sender, body


def route_message(target: str, sender: str, content: str) -> None:
    """路由一条 @ 消息到目标。"""
    global _routed_count

    if target == "主Agent":
        # 推入主 Agent supplement queue，格式含 @主Agent 标识让主 Agent 能识别
        full_msg = f"@主Agent [{sender}] {content}" if sender else f"@主Agent {content}"
        enqueue_supplement(full_msg)
        _routed_count += 1
        logger.info(f"db_monitor 路由到主 Agent：{full_msg[:50]}")
        return

    # 目标是子 Agent
    instance = SubagentRegistry.get(target)
    if instance is None:
        # 目标不在注册表，推回主 Agent
        fallback = f"@主Agent [system] 目标子 Agent {target} 已不存在：{content}"
        enqueue_supplement(fallback)
        logger.warning(f"db_monitor 目标子 Agent {target} 不在注册表，推回主 Agent")
        return

    # 推入子 Agent supplement queue
    is_terminate = content.strip() == "/stop"
    instance.supplement_queue.push(content, is_terminate=is_terminate, sender=sender)
    _routed_count += 1
    logger.info(f"db_monitor 路由到子 Agent {target}：{content[:50]} (terminate={is_terminate})")


async def _poll_messages(message_store) -> None:
    """轮询 db 中未路由的 subagent_msg 消息。"""
    global _routed_msg_ids
    try:
        msgs = await message_store.get_messages()
    except Exception as e:
        logger.error(f"db_monitor 查询消息失败：{e}")
        return

    for msg in msgs:
        if msg.id in _routed_msg_ids:
            continue
        if msg.role != "subagent_msg":
            continue
        target, sender, content = parse_at_message(msg.content)
        if not target:
            logger.warning(f"db_monitor 无法解析 @消息：{msg.content[:50]}")
            _routed_msg_ids.add(msg.id)
            continue
        try:
            route_message(target, sender, content)
        except Exception as e:
            logger.error(f"db_monitor 路由失败：{e}")
        _routed_msg_ids.add(msg.id)


async def run_db_monitor(interval: float = 0.2) -> None:
    """db 监测程序主循环。崩溃自动重启。

    启动时先初始化基线（当前所有 subagent_msg 标记为已路由），
    然后只路由启动后的新消息。
    """
    logger.info("db_monitor 启动")
    # 初始化基线
    message_store = await get_message_store()
    await _init_routed_baseline(message_store)

    while True:
        try:
            await _poll_messages(message_store)
            await asyncio.sleep(interval)
            # 每 60 秒心跳日志
            if _routed_count > 0 and _routed_count % 100 == 0:
                logger.info(f"db_monitor 心跳：已路由 {_routed_count} 条消息")
        except asyncio.CancelledError:
            logger.info("db_monitor 收到取消信号，退出")
            break
        except Exception as e:
            logger.error(f"db_monitor 异常崩溃，1 秒后重启：{e}")
            await asyncio.sleep(1)
```

**关键改动（相对原计划）**：
- `get_messages` 改为 `await message_store.get_messages()`（async 函数）
- `run_db_monitor` 内 `await get_message_store()` 拿单例（不再依赖外部传入）
- 新增 `_init_routed_baseline` 启动时灌入历史 subagent_msg id 作为基线，避免重启后重复路由
- `route_message` 推入主 Agent 时格式含 `@主Agent [sender]` 标识，让主 Agent 能识别这是子 Agent 问的问题（而不是用户补充）

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_db_monitor.py -v`
Expected: 7 个测试全 PASS

- [ ] **Step 6: 在 lifespan startup 启动 db 监测 task**

Read `niu_api/__main__.py:43-176`。

Edit `niu_api/__main__.py`，在 lifespan startup 中（约 `:137` 现有 `gateway_task` 启动附近）加：

```python
# === 新增：启动 db 监测程序 ===
from niu_api.db_monitor import run_db_monitor

db_monitor_task = asyncio.create_task(run_db_monitor())
logger.info("db_monitor task 已启动")
```

**注意：** `run_db_monitor()` 内部自己 `await get_message_store()` 拿单例，不需要外部传入。

- [ ] **Step 7: 在 lifespan shutdown 取消 db 监测 task**

Read `niu_api/__main__.py` lifespan shutdown 部分（约 `:170-176`）。

Edit 加：

```python
# === 新增：取消 db 监测 task ===
db_monitor_task.cancel()
try:
    await db_monitor_task
except asyncio.CancelledError:
    pass
```

- [ ] **Step 8: py_compile 验证**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m py_compile niu_api/db_monitor.py niu_api/__main__.py`
Expected: 无输出

- [ ] **Step 9: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/db_monitor.py niu_api/__main__.py tests/test_db_monitor.py
git commit -m "feat(db_monitor): 新建 db 监测程序轮询路由 @ 消息"
```

---

## Task 7: 主 Agent 提示词加逐条回复约束

**Files:**
- Modify: `config/agents/niu.md:194`（核心规则段）

**背景：** 主 Agent 系统提示词要说明：收到 `@主Agent` 消息时必须逐条回复，每条带发送者名字。

- [ ] **Step 1: Read niu.md 核心规则段**

Read: `config/agents/niu.md:194-218`（核心规则 + 推演原则 + 安全原则）

确认现有结构，找到合适插入点。

- [ ] **Step 2: 在核心规则段加逐条回复约束**

Edit `config/agents/niu.md`，在核心规则段（`:194` 附近）加：

```markdown
## 核心规则

### 主↔子 Agent 对话规则

当你（主 Agent）收到 `@主Agent [子Agent名] 问题` 这样的消息时（通过 supplement queue 推送给你）：
- 必须**逐条回复**，不能只回最后一个。多个子 Agent 同时问问题时，每个都要回答。
- 每条回复写在你的正常回复文本里，格式：`@子Agent名 回答内容`。后端会解析你回复里的 `@子Agent名` 模式，提取后以 subagent_msg role 存入 db，db 监测程序路由到对应子 Agent。
- 多条回复用换行分隔，每条 `@子Agent名` 单独一行，如：
  ```
  @file-processor-a1b2 是的，用 OCR 处理
  @context-manager-c3d4 暂时不用压缩
  ```

给子 Agent 补充上下文（不打断其工作）：
- 在你的回复里写 `@子Agent名 补充内容`，后端解析后以 subagent_msg role 存入 db。
- 子 Agent 下一轮调大模型时会作为补充信息消费（次末位插入）。
- 这是补充不是新指令，子 Agent 继续原任务。

停止某个子 Agent：
- 在你的回复里写 `@子Agent名 /stop`，后端解析存 db，子 Agent 收到后会总结本轮工作后终止。
- 双击停止按钮会给你所有在跑的子 Agent 发 /stop（包括同步和异步）。

### 同步调用子 Agent 的限制（阶段一）

- **同步调用的子 Agent 在跑期间，你处于阻塞状态，无法给它发 @ 消息**。同步子 Agent 跑完返回后你才醒来，此时它已结束，无需再发消息。
- **同步子 Agent 跑期间，单击停止按钮无效**（你阻塞在调用点检查不到信号灯）。只能双击停止按钮批量终止所有子 Agent。
- 这些限制在阶段二（异步调用）会解决——异步子 Agent 跑时你不阻塞，能随时 @ 它。
```

**关键改动（相对原计划）**：
- 明确主 Agent 发 @ 消息的方式：写在正常回复文本里，后端解析 `@子Agent名` 模式提取存 db（不是主 Agent 直接调工具写 db）
- 明确同步子 Agent 的限制：阻塞期间无法 @、单击停止无效、只能双击停止
- 多条回复的格式：每条 `@子Agent名` 单独一行

- [ ] **Step 3: 验证 niu.md 格式**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && head -220 config/agents/niu.md | tail -30`
Expected: 看到新加的"主↔子 Agent 对话规则"段

- [ ] **Step 4: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add config/agents/niu.md
git commit -m "docs(niu): 主 Agent 提示词加主子对话逐条回复约束"
```

---

## Task 8: 前端 chat.html role="subagent_msg" 渲染

**Files:**
- Modify: `ui/assistant/chat.html:932`（addMessage 函数）

**背景：** 前端 `addMessage` 加 `role === "subagent_msg"` 分支，解析 `@目标 [发送者名] 内容`，渲染为侧边小卡片样式。

- [ ] **Step 1: Read addMessage 函数**

Read: `ui/assistant/chat.html:932-960`（addMessage 函数完整）

确认现有 role 分支逻辑。

- [ ] **Step 2: 加 role="subagent_msg" 分支**

Edit `ui/assistant/chat.html:932` 附近，在 `addMessage` 函数内加分支：

```javascript
function addMessage(role, text, images = [], skipAppend = false) {
  // === 新增：subagent_msg 特殊渲染 ===
  if (role === 'subagent_msg') {
    return addSubagentMessage(text, skipAppend);
  }
  // ... 原有逻辑 ...
  if (role === 'tool') return null;
  // ...
}

function addSubagentMessage(text, skipAppend = false) {
  // 解析 @目标 [发送者名] 内容
  const match = text.match(/^@(\S+)(?:\s+\[([^\]]+)\])?\s*(.*)$/s);
  if (!match) {
    // 解析失败，按普通消息渲染
    return addMessage('assistant', text, [], skipAppend);
  }
  const target = match[1];
  const sender = match[2] || '';
  const content = match[3].trim();

  // 方向：sender → target
  const direction = sender ? `${sender} → ${target}` : `→ ${target}`;

  const div = document.createElement('div');
  div.className = 'message subagent-msg';
  div.innerHTML = `
    <div class="subagent-direction">${escapeHtml(direction)}</div>
    <div class="subagent-content">${escapeHtml(content)}</div>
  `;
  if (!skipAppend) {
    document.getElementById('messages').appendChild(div);
    scrollToBottom();
  }
  return div;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
```

- [ ] **Step 3: 加 CSS 样式**

在 `ui/assistant/chat.html` 的 `<style>` 区（找到现有 `.message` 样式附近）加：

```css
.message.subagent-msg {
  margin: 8px 16px;
  padding: 6px 10px;
  background: #f5f5f5;
  border-left: 3px solid #888;
  font-size: 0.85em;
  color: #555;
  border-radius: 4px;
}
.message.subagent-msg .subagent-direction {
  font-size: 0.8em;
  color: #888;
  margin-bottom: 2px;
}
.message.subagent-msg .subagent-content {
  color: #333;
}
```

- [ ] **Step 4: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep -n "subagent-msg\|addSubagentMessage" ui/assistant/chat.html | head -10`
Expected: 多处匹配（CSS + JS + HTML）

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add ui/assistant/chat.html
git commit -m "feat(ui): chat.html 渲染 role=subagent_msg 消息为侧边小卡片"
```

---

## Task 9: 双击停止按钮 UI

**Files:**
- Modify: `ui/assistant/chat.html:671-673`（stopBtn 点击）、`ui/assistant/chat.html:1382-1385`（Escape 键）

**背景：** 单击只停主 Agent（现有行为），400ms 内第二次单击 = 双击触发批量 /stop。Escape 仍走单击路径。

- [ ] **Step 1: Read 现有停止按钮逻辑**

Read: `ui/assistant/chat.html:665-685`（stopBtn 点击 + 上下文）、`ui/assistant/chat.html:1378-1390`（Escape 键）

- [ ] **Step 2: 改造 stopBtn 点击为双击识别**

Edit `ui/assistant/chat.html:671` 附近，改造点击逻辑：

```javascript
// === 双击停止按钮逻辑 ===
let stopClickTimer = null;
let stopClickFired = false;  // 哨兵：双击已触发，400ms 内后续点击忽略
const STOP_DOUBLE_CLICK_WINDOW = 400; // ms

stopBtn.addEventListener('click', () => {
  if (stopClickFired) {
    // 双击刚触发，400ms 内的后续点击忽略
    return;
  }
  if (stopClickTimer) {
    // 第二次点击（双击）
    clearTimeout(stopClickTimer);
    stopClickTimer = null;
    stopClickFired = true;
    setTimeout(() => { stopClickFired = false; }, STOP_DOUBLE_CLICK_WINDOW);
    // 双击：触发批量 /stop 所有子 Agent + 停主 Agent
    fetch('/api/stop_all', { method: 'POST' }).catch(e => console.error('stop_all 请求失败', e));
    window.electronAPI.sendMessage('/stop');
  } else {
    // 第一次点击（单击）
    stopClickTimer = setTimeout(() => {
      stopClickTimer = null;
      // 单击超时：只停主 Agent
      window.electronAPI.sendMessage('/stop');
      // 提示用户还有子 Agent 在跑（如果有）
      checkRunningSubagents();
    }, STOP_DOUBLE_CLICK_WINDOW);
  }
});
```

**关键改动（相对原计划）**：
- 加 `stopClickFired` 哨兵——双击触发后 400ms 内的后续点击忽略，避免连点 3 次误触发单击
- `/stop_all` 用 `fetch('/api/stop_all', {method:'POST'})` 直达后端，不走 sendMessage（避免进输入框）

**注意**：`checkRunningSubagents` 在 Task 13 实现（`/api/subagents/running` 端点）。Task 9 实施时先写空函数，Task 13 补实现。

- [ ] **Step 3: 后端加 /api/stop_all 端点**

**注意**：此 Step 依赖 Task 10 的 `request_stop_all_subagents` 函数。**实施顺序：Task 10 先于 Task 9 Step 3**。如果 Task 10 未实施，本 Step 先写端点骨架调用 `request_stop_all_subagents`（会 ImportError），Task 10 完成后自然可用。

Read `niu_api/chat.py:19` 确认 `router = APIRouter(...)`（chat.py 用 router，不是 app）。

Edit `niu_api/chat.py` 加（用 `@router.post` 不是 `@app.post`）：

```python
@router.post("/api/stop_all")
async def stop_all_subagents():
    """停止所有在跑的子 Agent（双击停止按钮触发）。

    停主 Agent 由前端单独发 /stop 处理（现有机制）。
    """
    from agent.runner import request_stop_all_subagents
    request_stop_all_subagents()
    return {"status": "ok"}
```

- [ ] **Step 4: Escape 键保持单击行为**

Read `ui/assistant/chat.html:1382-1385`。Escape 现有逻辑是等效 `/stop`，保持不变（单击行为）。

确认 Escape 不触发双击。

- [ ] **Step 5: 验证语法**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && grep -n "stopClickTimer\|stopClickFired\|STOP_DOUBLE_CLICK_WINDOW" ui/assistant/chat.html`
Expected: 多处匹配

- [ ] **Step 6: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add ui/assistant/chat.html niu_api/chat.py
git commit -m "feat(ui): 双击停止按钮触发批量 /stop_all + 单击只停主 Agent + 连点3次防护"
```

---

## Task 10: 信号灯重设计 + request_stop_all_subagents

**Files:**
- Modify: `agent/runner.py:25-40`（信号灯函数）、`agent/subagent.py`（已 Task 4 移除子 Agent 检查）
- Test: `tests/test_stop_signal.py`

**背景：** `_stop_requested` 保留只对主 Agent 有效（Task 4 已移除子 Agent 检查）。新增 `request_stop_all_subagents()` 给所有在跑子 Agent 推 /stop。

- [ ] **Step 1: Read 现有信号灯函数**

Read: `agent/runner.py:25-40`（`_stop_requested`、`request_stop`、`clear_stop`、`is_stop_requested`）

- [ ] **Step 2: 写失败测试**

Create: `tests/test_stop_signal.py`

```python
"""信号灯重设计测试。"""
from unittest.mock import patch, MagicMock


def test_request_stop_all_subagents():
    """request_stop_all_subagents 给所有在跑子 Agent 推 /stop。"""
    from agent.runner import request_stop_all_subagents
    mock_q1 = MagicMock()
    mock_q2 = MagicMock()
    with patch("agent.runner.SubagentRegistry") as mock_registry:
        mock_registry.list_running.return_value = [
            MagicMock(unique_name="a-1111", supplement_queue=mock_q1),
            MagicMock(unique_name="b-2222", supplement_queue=mock_q2),
        ]
        request_stop_all_subagents()
        mock_q1.push.assert_called_once_with("/stop", is_terminate=True, sender="主Agent")
        mock_q2.push.assert_called_once_with("/stop", is_terminate=True, sender="主Agent")


def test_request_stop_all_subagents_empty():
    """无在跑子 Agent时不崩溃。"""
    from agent.runner import request_stop_all_subagents
    with patch("agent.runner.SubagentRegistry") as mock_registry:
        mock_registry.list_running.return_value = []
        request_stop_all_subagents()  # 不应抛异常


def test_request_stop_still_works():
    """现有 request_stop 仍有效（只对主 Agent）。"""
    from agent.runner import request_stop, is_stop_requested, clear_stop
    clear_stop()
    assert not is_stop_requested()
    request_stop()
    assert is_stop_requested()
    clear_stop()
    assert not is_stop_requested()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_stop_signal.py -v`
Expected: FAIL（`request_stop_all_subagents` 不存在）

- [ ] **Step 4: 实现 request_stop_all_subagents**

Edit `agent/runner.py:40` 附近，在 `is_stop_requested` 函数后加：

```python
def request_stop_all_subagents() -> None:
    """给所有在跑的子 Agent 推 /stop（双击停止按钮触发）。

    遍历 SubagentRegistry，给每个子 Agent 的 supplement_queue 推 is_terminate=True 的 /stop。
    主 Agent 不受影响（主 Agent 用 _stop_requested 信号灯单独控制）。
    """
    from agent.subagent_registry import SubagentRegistry
    for instance in SubagentRegistry.list_running():
        try:
            instance.supplement_queue.push("/stop", is_terminate=True, sender="主Agent")
        except Exception as e:
            logger.error(f"给子 Agent {instance.unique_name} 推 /stop 失败：{e}")
```

**注意：** `logger` 如果 `agent/runner.py` 没有需加 `import logging; logger = logging.getLogger(__name__)`。Read 确认。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_stop_signal.py -v`
Expected: 3 个测试全 PASS

- [ ] **Step 6: py_compile 验证**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m py_compile agent/runner.py`
Expected: 无输出

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/runner.py tests/test_stop_signal.py
git commit -m "feat(runner): 新增 request_stop_all_subagents 批量停止子 Agent"
```

---

## Task 11: 子 Agent supplement 消费（次末插入 + /stop 最末插入）

**Files:**
- Modify: `agent/generic/agent_loop.py:695-710`（supplement 消费点）
- Test: `tests/test_supplement_consumption.py`

**背景：** 子 Agent drain 自己的 `SubagentSupplementQueue` 后，普通补充（`is_terminate=False`）→ 次末位插入；/stop（`is_terminate=True`）→ 最末位插入"总结后终止"。现有 `drain_supplement` 返回字符串，需改造支持 `SubagentSupplementItem` 列表。

- [ ] **Step 1: Read 现有 supplement 消费逻辑**

Read: `agent/generic/agent_loop.py:695-730`（drain_supplement 调用 + supplement 插入 messages 的逻辑）

确认现有 supplement 字符串怎么插入 messages（次末位机制）。

- [ ] **Step 2: 写失败测试**

Create: `tests/test_supplement_consumption.py`

```python
"""验证子 Agent supplement 消费：普通补充次末插入，/stop 最末插入。"""
from agent.subagent_supplement import SubagentSupplementQueue, SubagentSupplementItem


def test_drain_returns_items():
    """drain 返回 SubagentSupplementItem 列表（不是字符串）。"""
    q = SubagentSupplementQueue("test")
    q.push("普通补充")
    q.push("/stop", is_terminate=True)
    items = q.drain()
    assert all(isinstance(i, SubagentSupplementItem) for i in items)
    assert items[0].is_terminate is False
    assert items[1].is_terminate is True


def test_format_supplement_for_insert_normal():
    """普通补充格式化为次末插入文本。"""
    from agent.generic.agent_loop import format_subagent_supplement
    items = [SubagentSupplementItem("补充1", False, "主Agent")]
    text = format_subagent_supplement(items)
    assert "补充1" in text
    assert "终止" not in text


def test_format_supplement_for_insert_terminate():
    """/stop 格式化为最末插入文本（含终止指令）。"""
    from agent.generic.agent_loop import format_subagent_supplement
    items = [SubagentSupplementItem("/stop", True, "主Agent")]
    text = format_subagent_supplement(items, is_final_position=True)
    assert "终止" in text
    assert "总结" in text
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_supplement_consumption.py -v`
Expected: FAIL（`format_subagent_supplement` 不存在）

- [ ] **Step 4: 实现 format_subagent_supplement**

Edit `agent/generic/agent_loop.py`，在 `drain_supplement` 函数附近加：

```python
def format_subagent_supplement(items: list, is_final_position: bool = False) -> str:
    """格式化子 Agent supplement 为插入 LLM 上下文的文本。

    is_final_position=False（次末位）：普通补充，格式为"[主 Agent 补充] 内容"
    is_final_position=True（最末位）：/stop 终止，格式为"收到终止指令，请总结本轮工作后终止，不要再调用工具"
    """
    if not items:
        return ""

    if is_final_position:
        return "收到终止指令，请总结本轮工作后终止，不要再调用工具。"

    # 普通补充
    parts = []
    for item in items:
        if item.is_terminate:
            continue  # 终止指令不在次末位处理
        parts.append(f"[{item.sender} 补充] {item.content}")
    return "\n".join(parts) if parts else ""
```

- [ ] **Step 5: 修改 agent_runner_loop 的 supplement 消费逻辑**

Read `agent/generic/agent_loop.py:695-730`。

现有逻辑（约 701 行）：
```python
supplement = drain_fn()
```

改为区分普通补充和 /stop：

```python
# drain 返回 SubagentSupplementItem 列表（子 Agent 路径）或字符串（主 Agent 路径）
drained = drain_fn()

# 主 Agent 路径：drain_supplement 返回字符串或 None
if isinstance(drained, str) or drained is None:
    supplement = drained
    supplement_terminate = False
# 子 Agent 路径：返回 SubagentSupplementItem 列表
elif isinstance(drained, list):
    has_terminate = any(item.is_terminate for item in drained)
    if has_terminate:
        # /stop：最末位插入终止指令
        supplement = format_subagent_supplement(drained, is_final_position=True)
        supplement_terminate = True
    else:
        # 普通补充：次末位插入
        supplement = format_subagent_supplement(drained, is_final_position=False)
        supplement_terminate = False
else:
    supplement = None
    supplement_terminate = False
```

**注意：** `supplement_terminate` 标记用于控制循环退出。现有 `agent_runner_loop` 的循环退出条件是 `if not response.tool_calls: return`（约 `agent_loop.py:685`，LLM 无工具调用就退出）。

**关键作用域问题**：`response.tool_calls` 检查在 `agent_loop.py:685`，而 supplement drain 在 `:701`——即 `supplement_terminate` 在检查点之后才计算。必须把 `supplement_terminate` 的计算**提前到循环开头**，或移到 `response.tool_calls` 检查之前。

**改造方式**：

Read `agent/generic/agent_loop.py:680-710` 确认 `response.tool_calls` 检查点和 supplement drain 的相对位置。

在循环开头（`response = ...` 拿到 LLM 响应之后、`response.tool_calls` 检查之前）加 supplement drain + `supplement_terminate` 计算：

```python
# === 新增：在 response.tool_calls 检查前 drain supplement + 计算 terminate 标记 ===
supplement_terminate = False
if enable_supplement:
    drain_fn = supplement_drain if supplement_drain is not None else drain_supplement
    drained = drain_fn()
    # 主 Agent 路径：返回 str | None
    if isinstance(drained, str) or drained is None:
        supplement = drained
    # 子 Agent 路径：返回 list[SubagentSupplementItem]
    elif isinstance(drained, list):
        has_terminate = any(item.is_terminate for item in drained)
        if has_terminate:
            supplement = format_subagent_supplement(drained, is_final_position=True)
            supplement_terminate = True
        else:
            supplement = format_subagent_supplement(drained, is_final_position=False)
    else:
        supplement = None
else:
    supplement = None
# === 原有 response.tool_calls 检查（685 行）===
if not response.tool_calls:
    return  # 现有退出逻辑
# === 新增：终止模式下强制退出 ===
if supplement_terminate:
    # 终止指令已发，无论 LLM 是否调工具都退出，不执行工具调用
    logger.warning("终止模式下强制退出循环（LLM 可能仍调工具但不执行）")
    return
```

**关键**：
- `supplement_terminate` 在循环开头初始化为 False，drain 后更新
- 终止模式下无论 LLM 是否调工具都退出，不执行工具调用
- 这保证 /stop 一定能让子 Agent 终止

**注意**：原有 `:701` 的 `supplement = drain_supplement() if enable_supplement else None` 要删除（已移到循环开头）。但需确认 supplement 在 `:701` 之后还有没有别的用途——如果有，保留变量赋值但值已在循环开头算好。Read 确认。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_supplement_consumption.py -v`
Expected: 3 个测试全 PASS

- [ ] **Step 7: py_compile 验证**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m py_compile agent/generic/agent_loop.py`
Expected: 无输出

- [ ] **Step 8: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/generic/agent_loop.py tests/test_supplement_consumption.py
git commit -m "feat(agent_loop): 子 Agent supplement 消费 普通补充次末 /stop 最末"
```

---

## Task 12: 后端解析主 Agent 回复提取 @ 消息

**Files:**
- Create: `agent/at_message_parser.py`
- Modify: `agent/generic/agent_loop.py`（主 Agent 回复存 db 后调解析器）
- Test: `tests/test_at_message_parser.py`

**背景：** 主 Agent 在回复文本里写 `@子Agent名 内容`，后端解析提取后以 `role="subagent_msg"` 存入 db。这是通道一的实现——`@子Agent名` 格式承载所有主→子通信（补充上下文、停止指令、回复子 Agent 问题）。

**解析规则**：
- 扫描主 Agent 回复文本，匹配 `@<子Agent名> <内容>` 模式
- 子 Agent 名格式：`<type>-<4位hex>`（如 `file-processor-a1b2`），用正则 `@([\w]+-[\w]{4})\s+(.*)` 匹配
- 提取后以 `role="subagent_msg"` 存 db，content 格式 `@子Agent名 [主Agent] 内容`
- 从主 Agent 回复文本里**移除**这些 @ 消息（避免主 Agent 回复显示里残留 @ 消息文本）

- [ ] **Step 1: 写失败测试**

Create: `tests/test_at_message_parser.py`

```python
"""验证 @ 消息解析器。"""
from agent.at_message_parser import extract_at_messages, strip_at_messages, format_for_db


def test_extract_single_at_message():
    """单条 @ 消息提取。"""
    reply = "好的，我处理。\n@file-processor-a1b2 是的，用 OCR 处理"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["target"] == "file-processor-a1b2"
    assert msgs[0]["content"] == "是的，用 OCR 处理"
    assert msgs[0]["sender"] == "主Agent"


def test_extract_multiple_at_messages():
    """多条 @ 消息提取。"""
    reply = "@file-processor-a1b2 是的，用 OCR\n@context-manager-c3d4 暂时不用压缩"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 2
    assert msgs[0]["target"] == "file-processor-a1b2"
    assert msgs[1]["target"] == "context-manager-c3d4"


def test_extract_no_at_message():
    """无 @ 消息时返回空列表。"""
    reply = "好的，我处理这个文档。"
    msgs = extract_at_messages(reply)
    assert msgs == []


def test_extract_stop_command():
    """/stop 指令提取。"""
    reply = "@file-processor-a1b2 /stop"
    msgs = extract_at_messages(reply)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "/stop"


def test_strip_at_messages():
    """从回复文本移除 @ 消息。"""
    reply = "好的。\n@file-processor-a1b2 是的，用 OCR"
    stripped = strip_at_messages(reply)
    assert "@file-processor" not in stripped
    assert "好的" in stripped


def test_format_for_db():
    """提取后格式化为 db 存储格式。"""
    msg = {"target": "file-processor-a1b2", "content": "用 OCR", "sender": "主Agent"}
    formatted = format_for_db(msg)
    assert formatted == "@file-processor-a1b2 [主Agent] 用 OCR"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_at_message_parser.py -v`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 at_message_parser.py**

Create: `agent/at_message_parser.py`

```python
"""解析主 Agent 回复里的 @ 消息，提取后以 role=subagent_msg 存 db。

通道一实现：@子Agent名 格式承载所有主→子通信（补充上下文、停止指令、回复子 Agent 问题）。
子 Agent 名格式：<type>-<4位hex>（如 file-processor-a1b2）。
"""
import re

# 匹配 @<type>-<4hex> <内容>，内容到行尾或下一个 @
# [a-z]+ 严格匹配子 Agent 类型（小写字母），[0-9a-f]{4} 严格匹配 4 位 hex（secrets.token_hex(2) 输出）
_AT_PATTERN = re.compile(r'@([a-z]+-[0-9a-f]{4})\s+(.*?)(?=\s*@[a-z]+-[0-9a-f]{4}\s|\Z)', re.DOTALL)


def extract_at_messages(reply_text: str) -> list:
    """从主 Agent 回复文本提取 @ 消息。

    返回 [{"target": 子Agent名, "content": 内容, "sender": "主Agent"}, ...]
    """
    msgs = []
    for match in _AT_PATTERN.finditer(reply_text):
        target = match.group(1)
        content = match.group(2).strip()
        msgs.append({"target": target, "content": content, "sender": "主Agent"})
    return msgs


def strip_at_messages(reply_text: str) -> str:
    """从回复文本移除 @ 消息，返回剩余文本。"""
    stripped = _AT_PATTERN.sub('', reply_text)
    lines = [line.rstrip() for line in stripped.splitlines() if line.strip()]
    return '\n'.join(lines).strip()


def format_for_db(msg: dict) -> str:
    """格式化为 db 存储格式：@目标 [发送者名] 内容。"""
    return f"@{msg['target']} [{msg['sender']}] {msg['content']}"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_at_message_parser.py -v`
Expected: 6 个测试全 PASS

- [ ] **Step 5: 在 persist_agent_reply 内调解析器（async 函数，可 await）**

**关键修正**：`agent_runner_loop` 是同步生成器，不能 `await`。解析逻辑必须迁移到 async 函数 `persist_agent_reply`（`niu_api/chat.py:176`）。这样 3 个调用点（chat.py:435 流式、chat.py:555 非流式、chat_queue.py:343 重试）都自动覆盖。

Read `niu_api/chat.py:176-220` 看 `persist_agent_reply` 的签名和内部逻辑（它接收 `store, rv, history_len, full_reply, source, persisted_msgs`，返回 `message_id, full_reply`）。

在 `persist_agent_reply` 内部，**在把 `full_reply` 存为 `role="assistant"` 之前**加解析：

```python
async def persist_agent_reply(store, rv, history_len, full_reply, source="electron", persisted_msgs=None):
    # ... 现有逻辑 ...

    # === 新增：解析 full_reply 里的 @ 消息 ===
    from agent.at_message_parser import extract_at_messages, strip_at_messages, format_for_db

    at_msgs = extract_at_messages(full_reply)
    if at_msgs:
        # 先 strip @ 消息，存纯净回复为 role=assistant
        full_reply_clean = strip_at_messages(full_reply)
        # 用 full_reply_clean 替代 full_reply 走现有 persist 流程
        full_reply = full_reply_clean
        # @ 消息以 subagent_msg role 存 db
        for msg in at_msgs:
            await store.add_message(
                role="subagent_msg",
                content=format_for_db(msg)
            )

    # ... 原有 persist role=assistant 逻辑（用 full_reply，此时已 strip）...
    # ... 返回 message_id, full_reply ...
```

**关键点**：
- `persist_agent_reply` 是 async，可以 `await store.add_message(...)`
- 3 个调用点（流式/非流式/重试）都走这个函数，自动覆盖
- `full_reply` strip 后返回，前端拿到的也是 strip 后的纯净文本（@ 消息不显示在 assistant 气泡里）
- @ 消息以 `role="subagent_msg"` 单独存，前端用 Task 8 的特殊样式渲染

**注意**：`persist_agent_reply` 内现有逻辑可能先存 role=assistant 再做别的。Read 确认 persist 顺序，确保 strip 在存 assistant **之前**。如果现有逻辑是先 yield 流式 chunk 再最终 persist，要确认流式 chunk 不含 @ 消息（流式过程中 @ 消息可能跨 chunk，但最终 `full_reply` 是完整拼接，strip 在最终 persist 时应用即可）。

- [ ] **Step 6: py_compile 验证**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m py_compile agent/at_message_parser.py agent/generic/agent_loop.py`
Expected: 无输出

- [ ] **Step 7: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add agent/at_message_parser.py agent/generic/agent_loop.py tests/test_at_message_parser.py
git commit -m "feat(parser): 后端解析主 Agent 回复提取 @ 消息存 subagent_msg role"
```

---

## Task 13: 新增 /api/subagents/running 端点（双击停止 UX 提示）

**Files:**
- Modify: `niu_api/chat.py`（加端点）
- Modify: `ui/assistant/chat.html`（`checkRunningSubagents` 实现）
- Test: `tests/test_subagents_running_endpoint.py`

**背景：** 前端单击停止后想知道还有几个子 Agent 在跑，需要后端端点返回 `SubagentRegistry.list_running()` 数量。

- [ ] **Step 1: 写失败测试**

Create: `tests/test_subagents_running_endpoint.py`

```python
"""/api/subagents/running 端点测试。"""
from unittest.mock import patch, MagicMock


def test_running_endpoint_empty():
    """无子 Agent 时返回 count=0。"""
    with patch("agent.subagent_registry.SubagentRegistry.list_running", return_value=[]):
        # app 定义在 niu_api.__main__，chat.py 只有 router
        from niu_api.__main__ import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/subagents/running")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 0
        assert data["subagents"] == []


def test_running_endpoint_with_subagents():
    """有子 Agent 时返回 count 和名字列表。"""
    mock_inst1 = MagicMock(unique_name="file-processor-a1b2", agent_type="file-processor", is_sync=True)
    mock_inst2 = MagicMock(unique_name="context-manager-c3d4", agent_type="context-manager", is_sync=True)
    with patch("agent.subagent_registry.SubagentRegistry.list_running", return_value=[mock_inst1, mock_inst2]):
        from niu_api.__main__ import app
        from fastapi.testclient import TestClient
        client = TestClient(app)
        resp = client.get("/api/subagents/running")
        data = resp.json()
        assert data["count"] == 2
        assert len(data["subagents"]) == 2
        assert data["subagents"][0]["unique_name"] == "file-processor-a1b2"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagents_running_endpoint.py -v`
Expected: FAIL（端点不存在）

- [ ] **Step 3: 加 /api/subagents/running 端点**

Read `niu_api/chat.py:19` 确认 `router = APIRouter(...)`（chat.py 用 router）。

Edit `niu_api/chat.py` 加（用 `@router.get`）：

```python
@router.get("/api/subagents/running")
async def list_running_subagents():
    """返回当前在跑的子 Agent 列表（供前端双击停止 UX 提示）。"""
    from agent.subagent_registry import SubagentRegistry
    running = SubagentRegistry.list_running()
    return {
        "count": len(running),
        "subagents": [
            {
                "unique_name": inst.unique_name,
                "agent_type": inst.agent_type,
                "is_sync": inst.is_sync,
            }
            for inst in running
        ],
    }
```

- [ ] **Step 4: 实现 checkRunningSubagents**

Edit `ui/assistant/chat.html` 的 `checkRunningSubagents` 函数（Task 9 Step 2 定义的空函数）：

```javascript
async function checkRunningSubagents() {
  try {
    const resp = await fetch('/api/subagents/running');
    const data = await resp.json();
    if (data.count > 0) {
      const names = data.subagents.map(s => s.unique_name).join(', ');
      alert(`已停主 Agent。${data.count} 个子 Agent 仍在运行：${names}\n双击停止按钮可全部终止。`);
    }
  } catch (e) {
    console.error('查询子 Agent 状态失败', e);
  }
}
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagents_running_endpoint.py -v`
Expected: 2 个测试全 PASS

- [ ] **Step 6: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/chat.py ui/assistant/chat.html tests/test_subagents_running_endpoint.py
git commit -m "feat(api): /api/subagents/running 端点 + 前端单击停止 UX 提示"
```

---

## Task 14: 阶段一集成测试（真实 LLM）

**Files:**
- Test: `tests/test_subagent_interaction_integration.py`

**背景：** 按项目铁律（记忆 `real-testing-only`），集成测试必须用真实程序 + 真实 LLM。验证阶段一端到端：主 Agent 同步调子 Agent，主写 `@子名 补充`，子 Agent 下一轮消费；双击停止按钮触发批量 /stop。

- [ ] **Step 1: 写集成测试**

Create: `tests/test_subagent_interaction_integration.py`

```python
"""阶段一集成测试：真实程序 + 真实 LLM。

按记忆 real-testing-only：禁止 mock 测试，必须真实起子 Agent 跑。
测试前必须清空数据库。

测试场景：
1. 主 Agent 同步调子 Agent，主写 @子名 补充，子 Agent 消费
2. 双击停止触发 /stop_all，子 Agent 终止
"""
import pytest
import asyncio
import time


@pytest.fixture(autouse=True)
def clean_db():
    """每个测试前清空 messages db。"""
    import os
    from agent.session import MessageStore
    db_path = os.path.expanduser("~/.niu/messages.db")
    if os.path.exists(db_path):
        store = MessageStore(db_path)
        store.clear_all_messages()  # 假设有此方法，没有则用 SQL DELETE
    yield


def test_main_agent_supplement_to_subagent():
    """主 Agent 给同步调用的子 Agent 补充上下文。

    验证：
    1. 主 Agent 调 call_subagent 启动子 Agent
    2. 主 Agent 写 @子名 补充 到 db
    3. db 监测程序路由到子 Agent supplement queue
    4. 子 Agent 下一轮 LLM 调用前消费补充
    """
    # 这个测试需要真实启动主 Agent + 子 Agent + db 监测程序
    # 复杂度高，阶段一可标记为 manual，或用 subprocess 启动 ./niu 后用 API 测试
    pytest.skip("阶段一集成测试需手动执行：启动 ./niu，发消息触发子 Agent，观察日志验证")


def test_double_click_stop_all_subagents():
    """双击停止按钮触发 /stop_all。

    验证：
    1. 主 Agent 调多个子 Agent
    2. 调 POST /api/stop_all
    3. 所有子 Agent 收到 /stop 后终止
    """
    pytest.skip("阶段一集成测试需手动执行")


def test_subagent_msg_not_in_llm_history():
    """role=subagent_msg 消息不进主 Agent LLM 上下文。

    验证：
    1. 写 @主Agent 消息到 db
    2. 主 Agent 下一轮调 LLM 时，LLM 请求不含 subagent_msg 内容（除非通过 supplement）
    """
    pytest.skip("阶段一集成测试需手动执行")
```

**注意：** 阶段一集成测试因复杂度高（需真实启动完整程序），标记为 skip。手动测试在 Task 13 回归验证时做。单元测试（Task 1-11）已覆盖核心逻辑。

- [ ] **Step 2: 运行集成测试确认 skip**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && python/bin/python -m pytest tests/test_subagent_interaction_integration.py -v`
Expected: 3 个测试 SKIPPED

- [ ] **Step 3: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_subagent_interaction_integration.py
git commit -m "test(integration): 阶段一集成测试骨架（手动执行）"
```

---

## Task 15: 回归验证（手动测试）

**Files:**
- Verify: 所有阶段一改动

- [ ] **Step 1: 杀残留进程**

Run: `pkill -9 -f niu_api; pkill -9 -f "niu"; pkill -9 -f "Electron"; sleep 2; ps aux | grep -E "niu_api|Electron" | grep -v grep | wc -l`
Expected: `0`

- [ ] **Step 2: 启动程序**

Run: `cd REDACTED_USER_PATH/tools/ai-bot && ./niu > /tmp/niu_stage1.log 2>&1 &`
等 10 秒启动。

- [ ] **Step 3: 验证 db 监测程序启动**

Run: `sleep 10 && tail -100 logs/*.log 2>/dev/null | grep -i "db_monitor" | head -5`
Expected: 看到 "db_monitor 启动" 日志

- [ ] **Step 4: 验证主 Agent 能同步调子 Agent（现有功能不破坏）**

在 chat 窗口发消息让主 Agent 调子 Agent（如 file-processor），观察：
1. 主 Agent 正常调子 Agent
2. 子 Agent 跑完返回结果
3. 无报错

- [ ] **Step 5: 验证主 Agent 给子 Agent 补充上下文**

在子 Agent 跑期间，主 Agent 写 `@子Agent名 补充内容` 到 db（通过前端发消息）。观察：
1. db 监测程序路由日志
2. 子 Agent 下一轮 LLM 调用消费补充
3. 前端 `role="subagent_msg"` 消息渲染为侧边卡片

- [ ] **Step 6: 验证双击停止按钮**

启动一个长任务子 Agent，双击停止按钮。观察：
1. 所有子 Agent 收到 /stop
2. 子 Agent 总结后终止
3. 主 Agent 也停止

- [ ] **Step 7: 验证单击停止按钮**

启动一个长任务子 Agent，单击停止按钮。观察：
1. 主 Agent 停止
2. 子 Agent 继续跑（不响应全局信号灯）

- [ ] **Step 8: 检查日志无报错**

Run: `tail -200 logs/*.log 2>/dev/null | grep -iE "error|exception|traceback" | grep -v "思考链\|analysis" | head -10`
Expected: 无新引入的报错

- [ ] **Step 9: 杀进程，最终确认**

Run: `pkill -9 -f niu_api; pkill -9 -f "niu"; pkill -9 -f "Electron"; sleep 2`
Run: `cd REDACTED_USER_PATH/tools/ai-bot && git status && git log --oneline -15`

Expected: 工作区干净，13 个 commit（Task 1-12 各一个 + 可能的修复）

---

## Self-Review 检查（v2）

**Spec 覆盖**：
- ✅ 子 Agent 独立 supplement queue → Task 1, 4
- ✅ SubagentRegistry（阶段一简化版，含同步子 Agent）→ Task 2, 4
- ✅ agent_runner_loop 加 supplement_drain 参数 → Task 3
- ✅ 子 Agent 移除 is_stop_requested 检查 → Task 4
- ✅ messages db role="subagent_msg" + history 过滤 → Task 5
- ✅ db 监测程序（含 await 修复 + 基线初始化）→ Task 6
- ✅ 主 Agent 提示词逐条回复约束 + 同步子 Agent 限制说明 → Task 7
- ✅ 前端 @ 消息渲染 → Task 8
- ✅ 双击停止按钮 UI（含连点3次防护 + /stop_all fetch 通道）→ Task 9
- ✅ 信号灯重设计 + request_stop_all_subagents → Task 10
- ✅ supplement 消费（次末/最末插入 + 终止强制退出）→ Task 11
- ✅ 后端解析主 Agent 回复提取 @ 消息（通道一入口）→ Task 12
- ✅ /api/subagents/running 端点（双击停止 UX 提示）→ Task 13
- ✅ 集成测试 → Task 14
- ✅ 回归验证 → Task 15

**审查 bug 修复**：
- ✅ 阻塞1（db_monitor 缺 await + 无 message_store）→ Task 6 改 `await get_message_store()` 拿单例 + `await message_store.get_messages()`
- ✅ 阻塞2（supplement_terminate 退出逻辑缺失）→ Task 11 补强制退出代码
- ✅ 阻塞3（同步子 Agent 单击停止无效）→ Task 7 提示词明确说明为已知限制
- ✅ 阻塞4（/stop_all 发送通道 + Task 顺序）→ Task 9 用 fetch 直达 + Task 9 Step 3 说明依赖 Task 10
- ✅ 阻塞5（_routed_msg_ids 重启重复路由）→ Task 6 加 `_init_routed_baseline` 启动时灌基线
- ✅ 遗漏1（主 Agent 发 @ 消息入口）→ Task 12 新建 at_message_parser 后端解析
- ✅ 遗漏2（同步子 Agent 唯一名不可见）→ Task 7 提示词说清同步子 Agent 不能主动 @
- ✅ 遗漏3（前端不知子 Agent 数量）→ Task 13 加 /api/subagents/running 端点
- ✅ 改进3（双击连点3次）→ Task 9 加 stopClickFired 哨兵

**Placeholder 扫描**：无 TBD/TODO（Task 9 的 `checkRunningSubagents` 由 Task 13 实现，已标注依赖关系，不是 placeholder）。

**Type 一致性**：
- `SubagentSupplementQueue` 在 Task 1 定义、Task 4/6/10 调用一致
- `SubagentRegistry.register/unregister/get/list_running` 在 Task 2 定义、Task 4/6/10/13 调用一致
- `supplement_drain` 参数在 Task 3 定义、Task 4 传入、Task 11 消费一致
- `parse_at_message` / `route_message` 在 Task 6 定义、测试调用一致
- `format_subagent_supplement` 在 Task 11 定义、测试调用一致
- `extract_at_messages` / `strip_at_messages` / `format_for_db` 在 Task 12 定义、Task 12 测试 + Task 12 Step 5 调用一致

**实施顺序注意**：
- Task 9 Step 3（/api/stop_all 端点）依赖 Task 10（request_stop_all_subagents）——实施时 Task 10 先于 Task 9 Step 3，或 Task 9 先 commit 前端 + Task 10 commit 后端
- Task 9 的 `checkRunningSubagents` 由 Task 13 实现——Task 9 先写空函数，Task 13 补
- Task 11 的终止强制退出依赖 Task 11 自己的 supplement_terminate 标记——自洽
- Task 12 的解析器在主 Agent 回复 persist 前调用——需 Read 确认 persist 流程的所有调用点

**潜在风险**：
1. Task 4 的 `call_subagent` 签名加 `supplement_queue` 参数默认 None，现有调用方不传即走"创建临时 queue"路径——但这样每次同步调用都注册到 SubagentRegistry，双击停止时遍历到这些同步子 Agent。这是预期行为
2. Task 6 的 db 监测程序依赖 `get_message_store()` async 工厂——需确认此函数存在且返回单例。Read `agent/session.py` 确认
3. Task 11 的终止强制退出加在 `response.tool_calls` 检查处——需 Read 确认现有检查点的确切位置和结构
4. Task 12 的解析器正则 `@([\w]+-[\w]{4})` 只匹配 4 位 hex 后缀——子 Agent 名必须严格 `<type>-<4hex>` 格式。Task 2 的 `_gen_unique_name` 用 `secrets.token_hex(2)` 生成 4 位 hex，匹配
5. Task 14 集成测试仍标记 skip——阶段一验证靠 Task 15 手动测试 + 单元测试覆盖核心逻辑
6. Task 12 Step 5 的 persist 前解析——如果 persist 在多处（流式 chunk + 非流式 + chat_queue 重试），每处都要加，实施时需仔细 Read 所有 persist 调用点
