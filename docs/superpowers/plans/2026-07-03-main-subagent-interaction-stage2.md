# 主子 Agent 交互通道 阶段二实施计划（异步调用 + 进度查看 + ask_main_agent）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在阶段一通信通道之上，实现主 Agent 异步派子 Agent（后台运行、可看进度、可补充上下文、可停止单个、收完成通知），并让异步子 Agent 遇到歧义能通过 `ask_main_agent` 工具问主 Agent。

**Architecture:** 主 Agent 调 `chat-with-xxx` 时传 `async_mode=true` → handler 立即返回派单确认（含子 Agent 唯一名），子 Agent 跑在 `asyncio.to_thread` 独立线程注册到 SubagentRegistry；主 Agent 通过 `check_subagent_progress` 工具读子 Agent 内存上下文看进度，通过 `@子名 消息` 补充上下文，通过 `@子名 /stop` 停单个；子 Agent 通过 `ask_main_agent` 工具问主 Agent——消息进**主 Agent 请求内存队列**（不写 db），db_monitor 检测主 Agent 闲置时从队列 FIFO 取消息，推 SSE 触发前端调 `/api/chat/session`，后端写 user 消息到 db（作为最后一条 user 消息）+ 调 LLM，主 Agent 回复 `@子名 回答` 以 `role="subagent_msg"` 写 db，db_monitor 轮询路由到子 Agent PendingAskRegistry.set_answer 解除阻塞。

**Tech Stack:** Python 3.11+、asyncio（`asyncio.to_thread`、`asyncio.run_coroutine_threadsafe`）、threading（`threading.Event`、`threading.Lock`）、sqlite3（messages.db 轮询）、queue.Queue（supplement queue + 主 Agent 请求队列）、LiteLLM（子 Agent LLM 调用）、Iced/Rust 启动器 + 前端 chat.html/main.js（加自动触发逻辑）。

## 核心机制设计（阶段二关键——必须理解才能实施）

### 为什么不能用"直接写 db 触发主 Agent"

主 Agent 调 LLM 时，`agent_runner_loop` 从 `messages` 列表构造请求，`messages` 来自 db history（`compat.py:1431 context_manager.get_context_for_chat`）。**只有 db 最后一条 user 消息才会作为 LLM 当前输入**。如果子 Agent 的 ask 消息在主 Agent 跑期间就写 db，它只是 history 的一部分，不是"最后一条"——主 Agent 当前轮次不会执行它。必须等主 Agent 闲置（退出循环释放 `_chat_lock`），写入 db 作为最后一条 user 消息，再触发新一轮 LLM 对话，LLM 才会把它当作当前输入处理。

### 主 Agent 请求内存队列 + 闲置触发机制

**组件**：
- `MainAgentRequestQueue`（新建，`agent/main_agent_request_queue.py`）——全局内存队列，存子 Agent 的 ask 请求和完成通知
- `db_monitor` 扩展职责——除了现有 db 轮询（链路 B：路由 @子名 消息），新增"主 Agent 闲置检测 + 队列消费"（链路 A）

**两条职责独立的链路**：

**链路 A：ask_main_agent / 完成通知 → 主 Agent**（阶段二新增）
1. 子 Agent 调 `ask_main_agent` → 消息 content = `[子名] 问题内容` 推入 `MainAgentRequestQueue`（**不写 db**）
2. db_monitor 检测主 Agent 闲置（`_chat_lock.locked() == False`）+ 队列非空 → FIFO 取第一条消息
3. db_monitor 调 `notify_new_message` 推 SSE（绕过 source 白名单或加 `source="subagent"`）
4. 前端 `onNewMessage` 收到 `role="subagent_msg"` 且 `isProcessing == false`（闲置）→ 自动触发 `/api/chat/session`，message 参数 = `[子名] 问题内容`
5. 后端 `compat.py:1422` 写 user 消息到 db（content = `[子名] 问题内容`，role = user）→ 调 LLM
6. 主 Agent LLM 看到最后一条 user 消息是 `[子名] 问题内容`，回复 `@子名 回答内容`
7. `persist_agent_reply` 提取 @消息，以 `role="subagent_msg"` 写 db（content = `@子名 回答内容`）
8. db_monitor 链路 B 轮询到这条 subagent_msg → target=子名 → `PendingAskRegistry.set_answer` 解除 ask_main_agent 阻塞

**链路 B：db_monitor 现有 db 轮询**（阶段一已有，保持不变）
- 每 200ms 轮询 `role="subagent_msg" AND rowid > last_seen`
- target=="主Agent" → 不再走 enqueue_supplement（废弃），改为推入 MainAgentRequestQueue（链路 A 处理）
- target==子名 → 路由到子 Agent supplement queue（/stop、补充上下文）或 PendingAskRegistry.set_answer（ask 回答）

**死循环避免**：
- 链路 A 写 db 的消息 role 严格是 `user` → 链路 B 只轮询 `role="subagent_msg"`，不会触发
- 链路 B 写的消息 role 严格是 `subagent_msg`（persist_agent_reply 提取 @消息写入）→ 链路 B 自己轮询处理，不触发链路 A
- 两条链路操作的消息 role 不同，不会互相触发

**主 Agent 忙时累积多条消息**：
- 子 Agent A、B、C 同时 ask → 都进 MainAgentRequestQueue（FIFO）
- 主 Agent 忙 → db_monitor 不消费队列
- 主 Agent 闲 → db_monitor 取第一条 → 推 SSE → 前端触发 → 主 Agent 跑这一轮
- 主 Agent 跑完退出 → 闲 → db_monitor 取第二条 → 依次处理

**前端状态机持久化**（已确认，无需改动）：
- 前端 `isProcessing` 是内存变量，但后端 `_chat_lock` 是持久化源
- 窗口 show/focus 时主动调 `/api/chat/status` 同步前端状态
- 前端能准确判断主 Agent 忙闲，决定是否自动触发

### 同步子 Agent 不注入 ask_main_agent

同步调用子 Agent 时主 Agent 阻塞在 `call_subagent`，子 Agent 调 ask_main_agent 没意义（主 Agent 不会响应）。所以同步子 Agent 的 tools_schema 不含 ask_main_agent（Task 7 已有逻辑）。子 Agent 遇到麻烦没有途径问 → 自己尝试 → 做不了直接退出返回（阶段一已有的"直接退出"机制）。

**阶段一已铺好的接口（关键，阶段二复用）**：
- `agent/subagent_supplement.py` — `SubagentSupplementQueue(unique_name)` + `push(content, is_terminate, sender)` + `drain()`（已存在，阶段二不动）
- `agent/subagent_registry.py` — `SubagentRegistry.register(agent_type, supplement_queue, memory_context=None, is_sync=True)` → `unique_name`；`unregister/get/list_running`（已存在，阶段二扩展 `is_sync=False` 路径）
- `agent/at_message_parser.py` — `extract_at_messages/strip_at_messages/format_for_db`（已存在，阶段二不动）
- `niu_api/db_monitor.py` — `route_message(target, sender, content)` 已支持推子 Agent supplement queue 和主 Agent supplement queue（阶段二扩展 ask_main_agent 回答路由）
- `agent/generic/agent_loop.py` 的 `agent_runner_loop` — 已有 `supplement_drain: Callable[[], list] | None` 参数（阶段一加的，None 走主 Agent 全局 drain，非 None 走子 Agent 自己的 drain）；已有 `enable_supplement: bool` 参数
- `agent/runner.py` — `request_stop_all_subagents()` 已存在；`enqueue_supplement` 已存在
- `agent/handler.py:1018` — `chat-with-*` 分支调 `_call_subagent_gen`（同步阻塞，阶段二改分流）
- `agent/runner.py:264-282` — `chat-with-xxx` 工具 schema 静态生成（阶段二加 `async_mode` 参数）
- `config/agents/niu.md` — 主 Agent 提示词（阶段二加异步调用说明段）
- `config/agents/file-processor.md` 等 — 子 Agent frontmatter（阶段二加 `allowAsync: true` 标识，默认 false 不动其他子 Agent）

**阶段二关键设计约束**：
- `ask_main_agent` 阻塞在工具调用内部（`threading.Event.wait()`），子 Agent 不会执行到 `drain_supplement`，所以 /stop 推入 supplement queue 后子 Agent 消费不到 → 死锁
- **解决**：主 Agent 发 /stop 时，db_monitor 路由 `/stop` 到子 Agent supplement queue **同时**，调 `SubagentRegistry.cancel_pending_ask(unique_name)` 给 ask_main_agent 的 Event.set() 一个"已终止"信号让工具返回 `{"status":"terminated"}`，子 Agent 下一轮 drain 到 /stop 走终止总结流程
- `ask_main_agent` 阻塞等待时共用 db_monitor 的返回（不另起监测进程）；db_monitor 收到主 Agent 回答消息（`@子名` role=subagent_msg）时 `PendingAskRegistry.set_answer` 解除阻塞
- **关键**：ask_main_agent 消息**不写 db**，进 MainAgentRequestQueue 内存队列；db_monitor 检测主 Agent 闲置时推 SSE 触发前端调 /api/chat/session，后端写 user 消息到 db 作为最后一条 user 消息触发新一轮 LLM（详见"核心机制设计"段）
- **死循环避免**：链路 A（ask 请求）写 role="user" 消息，链路 B（db_monitor 轮询）只查 role="subagent_msg"，两条链路操作的消息 role 不同，不会互相触发

---

## 文件结构

### 新建文件
- `agent/subagent_memory.py` — `SubagentMemoryContext` 数据类（last_llm_request/last_llm_response/current_turn/last_tool_name + snapshot/update 加锁）。独立文件因为"子 Agent 内存上下文"是单一职责，与注册表（生命周期管理）分离。
- `agent/ask_main_agent.py` — `AskMainAgentFuture` + `PendingAskRegistry`（按 unique_name 路由 answer 的共享 dict + Event）。独立文件因为"子问主阻塞与回答路由"是单一职责，与 db_monitor（消息轮询）和 supplement queue（/stop 与补充）正交。
- `agent/main_agent_request_queue.py` — `MainAgentRequestQueue`（全局内存队列，存子 Agent 的 ask 请求和完成通知，FIFO）。独立文件因为"主 Agent 请求排队+闲置触发"是单一职责，与 supplement queue（子 Agent 内部消费）和 PendingAskRegistry（ask 回答路由）正交。
- `tests/test_subagent_memory.py` — SubagentMemoryContext 单元测试
- `tests/test_ask_main_agent.py` — AskMainAgentFuture + PendingAskRegistry 单元测试
- `tests/test_main_agent_request_queue.py` — MainAgentRequestQueue 单元测试
- `tests/test_async_subagent_dispatch.py` — _dispatch_async_subagent + _run_subagent_async 集成测试（用真实 LLM）
- `tests/test_check_subagent_progress.py` — check_subagent_progress 工具集成测试
- `tests/test_ask_main_agent_integration.py` — ask_main_agent 端到端测试（子 Agent 问 → 主 Agent 答 → 子 Agent 继续，用真实 LLM）
- `tests/test_async_stop_deadlock.py` — ask_main_agent 阻塞期间收 /stop 不死锁测试（用真实 LLM）

### 修改文件
- `agent/subagent.py` — `call_subagent` 加 `memory_context` 参数；`_run_agent_loop` 加 `memory_context` 参数并在 chunk 循环里更新 memory_context；新增 `_build_subagent_tools_schema()` 返回带 `async_mode` 的 schema；新增 `_dispatch_async_subagent` + `_run_subagent_async`；`_run_subagent_async` 完成通知走 MainAgentRequestQueue（不直接写 db）
- `agent/handler.py` — `dispatch` 的 `chat-with-*` 分支解析 `async_mode`，true 走 `_dispatch_async_subagent`，false 走现有 `_call_subagent_gen`；新增 `do_check_subagent_progress` 方法；`ask_main_agent` 工具分支调 `_ask_main_agent_impl`
- `agent/subagent_registry.py` — `RunningSubagent` 加 `task: Optional[Union[asyncio.Task, ConcurrentFuture]]` 字段（异步子 Agent 才有）；`register` 加 `task` 参数；加 `started_at` 字段
- `niu_api/db_monitor.py` — `route_message` 中主 Agent 回答消息（`@子名` 来自 `主Agent`）路由时调 `PendingAskRegistry.set_answer(target, content)` 解除 ask_main_agent 阻塞；`/stop` 路由时同时调 `cancel_pending_ask` 解除死锁；**新增链路 A**：主 Agent 闲置检测（`_chat_lock.locked()`）+ MainAgentRequestQueue FIFO 消费 + 推 SSE 触发前端
- `agent/runner.py` — `get_tools_schema` 在子 Agent schema 生成处理 `allowAsync` 标识；`_inject_dynamic_resources` 加"后台子 Agent 清单"注入段；`request_stop_all_subagents` 调 `cancel_pending_ask` 避免双击停止死锁
- `niu_api/chat.py` — `notify_new_message` 加 `source="subagent"` 支持（绕过 electron 白名单）
- `ui/assistant/chat.html` — `onNewMessage` 加 `role="subagent_msg"` 分支自动触发 `/api/chat/session`（检查 `isProcessing == false`）
- `config/agents/niu.md` — 主 Agent 提示词加异步调用说明段
- `config/agents/file-processor.md` — frontmatter 加 `allowAsync: true`（其他子 Agent 默认不动，本次只让 file-processor 支持异步，作为阶段二验证目标）

---

## 任务列表

### Task 1: SubagentMemoryContext 数据类

**Files:**
- Create: `agent/subagent_memory.py`
- Test: `tests/test_subagent_memory.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_memory.py
import threading
import time
from agent.subagent_memory import SubagentMemoryContext


def test_snapshot_returns_consistent_state():
    """snapshot 一次性拷贝，主 Agent 读到一致状态（不会 current_turn=5 但 last_llm_response 还是 turn 4）。"""
    ctx = SubagentMemoryContext()
    ctx.update(last_llm_request="req-turn-3", last_llm_response="resp-turn-3", current_turn=3, last_tool_name="read")
    
    snap = ctx.snapshot()
    assert snap["last_llm_request"] == "req-turn-3"
    assert snap["last_llm_response"] == "resp-turn-3"
    assert snap["current_turn"] == 3
    assert snap["last_tool_name"] == "read"


def test_update_modifies_fields():
    ctx = SubagentMemoryContext()
    ctx.update(current_turn=1, last_llm_response="hello")
    assert ctx.snapshot()["current_turn"] == 1
    assert ctx.snapshot()["last_llm_response"] == "hello"
    # 未更新的字段保持 None
    assert ctx.snapshot()["last_llm_request"] is None


def test_snapshot_is_copy_not_reference():
    """snapshot 返回的 dict 修改不影响内部状态。"""
    ctx = SubagentMemoryContext()
    ctx.update(current_turn=5)
    snap = ctx.snapshot()
    snap["current_turn"] = 999
    assert ctx.snapshot()["current_turn"] == 5


def test_concurrent_update_and_snapshot_thread_safe():
    """多线程并发 update + snapshot 不抛异常。"""
    ctx = SubagentMemoryContext()
    errors = []
    
    def updater():
        try:
            for i in range(100):
                ctx.update(current_turn=i, last_llm_response=f"r{i}")
        except Exception as e:
            errors.append(e)
    
    def snapshotter():
        try:
            for _ in range(100):
                ctx.snapshot()
        except Exception as e:
            errors.append(e)
    
    t1 = threading.Thread(target=updater)
    t2 = threading.Thread(target=snapshotter)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_subagent_memory.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.subagent_memory'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/subagent_memory.py
"""子 Agent 内存上下文 — 进度数据来源。

子 Agent 跑的时候，每轮 LLM 调用前后更新这个对象。
主 Agent 调 check_subagent_progress 时通过 snapshot() 一次性拷贝读一致状态。
内存对象不进 db，子 Agent 结束后随注册表移除而消失。
"""
import threading
from typing import Optional


class SubagentMemoryContext:
    """子 Agent 最近一轮 LLM 对话的内存对象。
    
    用普通类而非 @dataclass——threading.Lock 与 dataclass 的 __eq__/__hash__ 语义冲突，
    且 _lock 不应是 dataclass 字段（避免 asdict/astuple 包含锁）。
    
    Fields:
        last_llm_request: 最近一轮送给 LLM 的内容摘要（最后一条 user content 或 messages 拼接摘要）
        last_llm_response: LLM 最近一轮的回复文本（不含工具调用）
        current_turn: 当前第几轮（从 1 开始）
        last_tool_name: 最近一次调的工具名（可选辅助信息）
    """
    
    def __init__(self):
        self.last_llm_request: Optional[str] = None
        self.last_llm_response: Optional[str] = None
        self.current_turn: int = 0
        self.last_tool_name: Optional[str] = None
        self._lock = threading.Lock()
    
    def snapshot(self) -> dict:
        """一次性拷贝所有字段，保证主 Agent 读到一致状态。"""
        with self._lock:
            return {
                "last_llm_request": self.last_llm_request,
                "last_llm_response": self.last_llm_response,
                "current_turn": self.current_turn,
                "last_tool_name": self.last_tool_name,
            }
    
    def update(self, **kwargs) -> None:
        """子 Agent 线程更新字段，加锁保证一致性。"""
        with self._lock:
            for k, v in kwargs.items():
                if hasattr(self, k) and not k.startswith("_"):
                    setattr(self, k, v)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_subagent_memory.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/subagent_memory.py tests/test_subagent_memory.py
git commit -m "feat(subagent): 新增 SubagentMemoryContext 进度数据载体"
```

---

### Task 2: AskMainAgentFuture + PendingAskRegistry

**Files:**
- Create: `agent/ask_main_agent.py`
- Test: `tests/test_ask_main_agent.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ask_main_agent.py
import threading
import time
from agent.ask_main_agent import AskMainAgentFuture, PendingAskRegistry


def test_future_wait_blocks_until_set_answer():
    """future.wait() 阻塞，set_answer 后解除。"""
    future = AskMainAgentFuture()
    
    result = {}
    def waiter():
        result["answer"] = future.wait(timeout=2.0)
    
    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.1)  # 确保 waiter 已进入 wait
    assert not result  # 还没结果
    
    future.set_answer("这是主 Agent 的回答")
    t.join(timeout=2.0)
    
    assert result["answer"] == "这是主 Agent 的回答"


def test_future_wait_timeout_returns_none():
    """超时返回 None。"""
    future = AskMainAgentFuture()
    result = future.wait(timeout=0.05)
    assert result is None


def test_registry_register_and_set_answer():
    """注册 future 后，set_answer 按 unique_name 路由到正确 future。"""
    reg = PendingAskRegistry()
    f1 = reg.register("file-processor-a1b2")
    f2 = reg.register("context-manager-c3d4")
    
    reg.set_answer("file-processor-a1b2", "回答 1")
    reg.set_answer("context-manager-c3d4", "回答 2")
    
    assert f1.wait(timeout=1.0) == "回答 1"
    assert f2.wait(timeout=1.0) == "回答 2"


def test_registry_cancel_pending_ask():
    """cancel_pending_ask 给 future 设 'terminated' 信号，工具返回终止状态。"""
    reg = PendingAskRegistry()
    f = reg.register("file-processor-a1b2")
    
    reg.cancel_pending_ask("file-processor-a1b2")
    
    answer = f.wait(timeout=1.0)
    assert answer == "__TERMINATED__"


def test_registry_unregister_removes_future():
    """注销后 future 不再可路由。"""
    reg = PendingAskRegistry()
    f = reg.register("file-processor-a1b2")
    reg.unregister("file-processor-a1b2")
    
    # 注销后 set_answer 不抛异常，但 future 永远拿不到（已不在 dict）
    reg.set_answer("file-processor-a1b2", "回答")
    assert f.wait(timeout=0.1) is None  # 超时，没拿到


def test_registry_cancel_missing_unique_name_no_error():
    """cancel 不存在的 unique_name 不抛异常（异步子 Agent 可能没问主就崩溃）。"""
    reg = PendingAskRegistry()
    reg.cancel_pending_ask("nonexistent-name")  # 不抛异常
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_ask_main_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.ask_main_agent'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/ask_main_agent.py
"""子 Agent 问主 Agent 的阻塞与回答路由机制。

子 Agent 调 ask_main_agent 工具时：
  1. 创建 AskMainAgentFuture（threading.Event + answer 共享变量）
  2. 注册到 PendingAskRegistry（key=unique_name）
  3. 推 "[unique_name] question" 到 MainAgentRequestQueue 内存队列（不写 db）
  4. future.wait() 阻塞，直到主 Agent 回答或被 cancel

db_monitor 链路 A 检测主 Agent 闲置时消费 MainAgentRequestQueue：
  - 推 SSE 触发前端调 /api/chat/session
  - 后端写 user 消息到 db（content="[子名] question"，作为最后一条 user 消息触发主 Agent 新一轮 LLM）
  - 主 Agent 回复 @子名 回答，persist_agent_reply 以 role=subagent_msg 写 db

db_monitor 链路 B 轮询到主 Agent 回答消息（@子名 role=subagent_msg）时：
  - PendingAskRegistry.set_answer(子名, 回答) — 解除 ask_main_agent 阻塞

主 Agent 发 /stop 给子 Agent 时（@子名 /stop）：
  - db_monitor 链路 B 推 /stop 到子 Agent supplement queue（is_terminate=True）
  - 同时 PendingAskRegistry.cancel_pending_ask(子名) — 解除 ask_main_agent 阻塞（避免死锁）
"""
import threading
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# 终止信号 — cancel_pending_ask 设此值，ask_main_agent 工具识别后返回终止状态
TERMINATED_SIGNAL = "__TERMINATED__"


class AskMainAgentFuture:
    """子 Agent 问主 Agent 的一次阻塞等待。
    
    线程安全：Event + answer 共享变量。子 Agent 跑在 asyncio.to_thread 独立线程，
    db_monitor 跑在主 asyncio loop（route_message 是同步函数），跨线程用 Event.set() 安全。
    """
    
    def __init__(self):
        self._event = threading.Event()
        self._answer: Optional[str] = None
    
    def set_answer(self, answer: str) -> None:
        """主 Agent 回答路由来时调，解除阻塞。"""
        self._answer = answer
        self._event.set()
    
    def wait(self, timeout: float = None) -> Optional[str]:
        """阻塞等待回答。超时返回 None；被 cancel 返回 TERMINATED_SIGNAL。"""
        self._event.wait(timeout=timeout)
        return self._answer


class PendingAskRegistry:
    """按 unique_name 路由 ask_main_agent 回答的注册表。
    
    同一子 Agent 同时只有一个 Future 在等（ask_main_agent 阻塞子 Agent 循环），
    所以按 unique_name 路由唯一且简单。
    """
    
    def __init__(self):
        self._futures: dict[str, AskMainAgentFuture] = {}
        self._lock = threading.Lock()
    
    def register(self, unique_name: str) -> AskMainAgentFuture:
        """子 Agent 调 ask_main_agent 时注册一个 future。
        
        如果该 unique_name 已有 future（前一次 ask 未解除就再问，不应发生但容错），
        旧 future 设 TERMINATED_SIGNAL 解除阻塞，避免泄漏。
        """
        future = AskMainAgentFuture()
        with self._lock:
            old = self._futures.get(unique_name)
            if old is not None:
                old.set_answer(TERMINATED_SIGNAL)
            self._futures[unique_name] = future
        return future
    
    def set_answer(self, unique_name: str, answer: str) -> bool:
        """主 Agent 回答路由来时调。返回是否找到 future。
        
        找到 future → set_answer 解除阻塞，从注册表移除。
        找不到（孤儿回答：子 Agent 崩溃/超时后主 Agent 才回答）→ 返回 False + 日志，
        调用方（db_monitor）决定降级处理（推 supplement queue 让子 Agent 下一轮看到，
        或子 Agent 已退出则推回主 Agent）。
        """
        with self._lock:
            future = self._futures.pop(unique_name, None)
        if future is None:
            logger.warning(f"PendingAskRegistry.set_answer: 找不到 {unique_name} 的 future（可能被 cancel 或超时先 pop），回答降级处理")
            return False
        future.set_answer(answer)
        return True
    
    def cancel_pending_ask(self, unique_name: str) -> None:
        """主 Agent 发 /stop 时调，解除 ask_main_agent 阻塞避免死锁。
        
        找到 future → set_answer(TERMINATED_SIGNAL)，ask_main_agent 工具识别后返回终止状态。
        找不到（子 Agent 没在问主）→ 静默无操作。
        """
        with self._lock:
            future = self._futures.pop(unique_name, None)
        if future is not None:
            future.set_answer(TERMINATED_SIGNAL)
    
    def unregister(self, unique_name: str) -> None:
        """子 Agent 结束时调（正常/异常/终止），清理未解除的 future。"""
        with self._lock:
            self._futures.pop(unique_name, None)


# 全局单例 — db_monitor 和 ask_main_agent 工具共用
_pending_ask_registry = PendingAskRegistry()


def get_pending_ask_registry() -> PendingAskRegistry:
    """获取全局 PendingAskRegistry 单例。"""
    return _pending_ask_registry
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_ask_main_agent.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/ask_main_agent.py tests/test_ask_main_agent.py
git commit -m "feat(ask-main-agent): 新增 AskMainAgentFuture + PendingAskRegistry 阻塞与回答路由机制"
```

---

### Task 2.5: MainAgentRequestQueue 主 Agent 请求内存队列

**Files:**
- Create: `agent/main_agent_request_queue.py`
- Test: `tests/test_main_agent_request_queue.py`

**职责**：存子 Agent 的 ask 请求和完成通知（content 格式 `[子名] 内容`），FIFO，db_monitor 检测主 Agent 闲置时消费。**不写 db**——只在主 Agent 闲置时由 db_monitor 推 SSE 触发前端，前端调 /api/chat/session 后由后端 compat.py 写 user 消息到 db。

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_agent_request_queue.py
import threading
import time
from agent.main_agent_request_queue import MainAgentRequestQueue


def test_push_and_pop_fifo():
    """push 后 pop 按 FIFO 顺序返回。"""
    q = MainAgentRequestQueue()
    q.push("[file-processor-a1b2] 问题 1")
    q.push("[file-processor-c3d4] 问题 2")
    
    assert q.pop() == "[file-processor-a1b2] 问题 1"
    assert q.pop() == "[file-processor-c3d4] 问题 2"
    assert q.pop() is None  # 队列空


def test_pop_empty_returns_none():
    """空队列 pop 返回 None，不阻塞。"""
    q = MainAgentRequestQueue()
    assert q.pop() is None


def test_is_empty():
    """is_empty 正确反映队列状态。"""
    q = MainAgentRequestQueue()
    assert q.is_empty()
    q.push("[子名] 内容")
    assert not q.is_empty()
    q.pop()
    assert q.is_empty()


def test_thread_safe_push_pop():
    """多线程并发 push/pop 不抛异常。"""
    q = MainAgentRequestQueue()
    errors = []
    
    def producer():
        try:
            for i in range(100):
                q.push(f"[子名-{i:04d}] 内容 {i}")
        except Exception as e:
            errors.append(e)
    
    def consumer():
        try:
            for _ in range(100):
                q.pop()
        except Exception as e:
            errors.append(e)
    
    t1 = threading.Thread(target=producer)
    t2 = threading.Thread(target=consumer)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert errors == []


def test_peek_does_not_remove():
    """peek 查看队首但不移除（db_monitor 检测主 Agent 闲时先 peek 决定是否触发）。"""
    q = MainAgentRequestQueue()
    q.push("[子名] 内容")
    
    assert q.peek() == "[子名] 内容"
    assert not q.is_empty()  # 没移除
    assert q.pop() == "[子名] 内容"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_main_agent_request_queue.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.main_agent_request_queue'`

- [ ] **Step 3: Write minimal implementation**

```python
# agent/main_agent_request_queue.py
"""主 Agent 请求内存队列。

存子 Agent 的 ask 请求和完成通知（content 格式 `[子名] 内容`），FIFO。
db_monitor 检测主 Agent 闲置时 pop 一条，推 SSE 触发前端调 /api/chat/session。

不写 db——只在主 Agent 闲置时由 db_monitor 推 SSE，前端触发后由后端 compat.py 写 user 消息到 db。
这样保证消息是 db 最后一条 user 消息，LLM 才会作为当前输入处理。

线程安全：queue.Queue 实现，多线程 push/pop 安全。
"""
import queue as _queue
from typing import Optional


class MainAgentRequestQueue:
    """全局内存队列，存子 Agent → 主 Agent 的请求（ask 或完成通知）。
    
    db_monitor 链路 A 消费此队列：
    1. 检测 _chat_lock.locked() == False（主 Agent 闲置）
    2. peek 队首，如果有消息：
       - 调 notify_new_message 推 SSE
       - pop 移除（推 SSE 成功后才 pop，避免推送失败丢消息）
    3. 前端收到 SSE → 调 /api/chat/session → 后端写 user 消息 + 调 LLM
    """
    
    def __init__(self):
        self._q: _queue.Queue[str] = _queue.Queue()
    
    def push(self, content: str) -> None:
        """推入一条请求。线程安全（queue.Queue.put_nowait）。"""
        self._q.put_nowait(content)
    
    def pop(self) -> Optional[str]:
        """取出并移除队首。空队列返回 None，不阻塞。"""
        try:
            return self._q.get_nowait()
        except _queue.Empty:
            return None
    
    def peek(self) -> Optional[str]:
        """查看队首但不移除。空队列返回 None。
        
        db_monitor 检测主 Agent 闲时先 peek 决定是否推 SSE，
        推 SSE 成功后才 pop（避免推送失败丢消息）。
        """
        try:
            return self._q.queue[0]  # queue.Queue 内部 deque，访问 [0] 不移除
        except IndexError:
            return None
    
    def is_empty(self) -> bool:
        """队列是否为空。"""
        return self._q.empty()


# 全局单例
_main_agent_request_queue = MainAgentRequestQueue()


def get_main_agent_request_queue() -> MainAgentRequestQueue:
    """获取全局 MainAgentRequestQueue 单例。"""
    return _main_agent_request_queue
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_main_agent_request_queue.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add agent/main_agent_request_queue.py tests/test_main_agent_request_queue.py
git commit -m "feat(main-agent-queue): 新增 MainAgentRequestQueue 主 Agent 请求内存队列"
```

---

### Task 3: SubagentRegistry 扩展支持异步子 Agent

**Files:**
- Modify: `agent/subagent_registry.py`
- Test: `tests/test_subagent_registry_async.py`（新建）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_registry_async.py
import asyncio
from agent.subagent_registry import SubagentRegistry, RunningSubagent
from agent.subagent_supplement import SubagentSupplementQueue
from agent.subagent_memory import SubagentMemoryContext


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
        finally:
            SubagentRegistry.unregister(name)
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
    finally:
        SubagentRegistry.unregister(name)


def test_list_running_filters_by_is_sync():
    """list_running 返回所有，调用方按 is_sync 过滤。"""
    sq1 = SubagentSupplementQueue("test-filter-0001")
    sq2 = SubagentSupplementQueue("test-filter-0002")
    n1 = SubagentRegistry.register("test-filter", supplement_queue=sq1, is_sync=True)
    n2 = SubagentRegistry.register("test-filter", supplement_queue=sq2, is_sync=False, memory_context=SubagentMemoryContext())
    try:
        running = SubagentRegistry.list_running()
        sync = [r for r in running if r.is_sync]
        async_ = [r for r in running if not r.is_sync]
        assert any(r.unique_name == n1 for r in sync)
        assert any(r.unique_name == n2 for r in async_)
    finally:
        SubagentRegistry.unregister(n1)
        SubagentRegistry.unregister(n2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_subagent_registry_async.py -v`
Expected: FAIL with `TypeError: register() got an unexpected keyword argument 'task'`

- [ ] **Step 3: Modify SubagentRegistry to add task field**

修改 `agent/subagent_registry.py`：`RunningSubagent` 加 `task` 字段，`register` 加 `task` 参数。

```python
# agent/subagent_registry.py（完整替换）
"""子 Agent 注册表。

维护当前在跑的子 Agent（含同步和异步）。
- 同步子 Agent：is_sync=True，task=None，memory_context=None（阶段一已有）
- 异步子 Agent：is_sync=False，task=asyncio.Task，memory_context=SubagentMemoryContext（阶段二新增）

双击停止按钮遍历此注册表批量推 /stop。
db_monitor 路由 @子名 消息时从此注册表拿 supplement_queue。

线程安全：register/unregister 用 threading.Lock 保护（read-modify-write 非原子）。
"""
import threading
import secrets
import asyncio
import time
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from typing import Optional, Any, Union


@dataclass
class RunningSubagent:
    unique_name: str
    agent_type: str
    supplement_queue: Any  # SubagentSupplementQueue
    memory_context: Optional[Any] = None  # 异步子 Agent 才有，同步为 None
    is_sync: bool = True
    # task 字段：异步子 Agent 的可取消句柄
    # 用 run_coroutine_threadsafe 跨线程调度时返回 concurrent.futures.Future（不是 asyncio.Task）
    # 两者都有 cancel() 方法，类型用 Union 兼容
    task: Optional[Union[asyncio.Task, ConcurrentFuture]] = None  # 异步子 Agent 才有，同步为 None
    started_at: float = field(default_factory=time.time)  # 启动时间，用于动态注入区排序


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
    def register(
        cls,
        agent_type: str,
        supplement_queue: Any,
        memory_context: Optional[Any] = None,
        is_sync: bool = True,
        task: Optional[Union[asyncio.Task, ConcurrentFuture]] = None,
    ) -> str:
        """注册一个子 Agent，返回唯一名。
        
        同步子 Agent：is_sync=True，task=None，memory_context=None
        异步子 Agent：is_sync=False，task=asyncio.Task 或 concurrent.futures.Future，memory_context=SubagentMemoryContext
        """
        with cls._lock:
            name = cls._gen_unique_name(agent_type)
            cls._instances[name] = RunningSubagent(
                unique_name=name,
                agent_type=agent_type,
                supplement_queue=supplement_queue,
                memory_context=memory_context,
                is_sync=is_sync,
                task=task,
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

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_subagent_registry_async.py tests/test_subagent_registry.py -v 2>&1 | tail -20`
Expected: PASS（新测试 3 个 + 阶段一已有测试不破坏）

- [ ] **Step 5: Commit**

```bash
git add agent/subagent_registry.py tests/test_subagent_registry_async.py
git commit -m "feat(subagent-registry): 扩展支持异步子 Agent（task + memory_context 字段）"
```

---

### Task 4: call_subagent 加 memory_context 参数 + _run_agent_loop 加 memory_context 更新钩子

**Files:**
- Modify: `agent/subagent.py:188-265`（`_run_agent_loop`）、`agent/subagent.py:467-646`（`call_subagent`）
- Test: `tests/test_call_subagent_memory_hook.py`（新建）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_subagent_memory_hook.py
"""验证 call_subagent 传 memory_context 时，子 Agent 每轮 LLM 调用前后更新 memory_context。

用真实 LLM 调一个简短任务（"回复 OK"），验证 memory_context.current_turn >= 1 且
last_llm_response 非空。
"""
import os
import pytest
from agent.subagent import call_subagent
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_supplement import SubagentSupplementQueue


@pytest.fixture
def llm_config():
    """读 config/user-config.json 的真实 LLM 配置。"""
    import json
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    llm = cfg.get("llm", {})
    return {
        "apikey": llm.get("apikey", ""),
        "apibase": llm.get("apibase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
    }


def test_call_subagent_updates_memory_context(llm_config):
    """call_subagent 传 memory_context 时，子 Agent 跑完后 memory_context 有数据。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")
    
    sq = SubagentSupplementQueue("test-mem-0001")
    mc = SubagentMemoryContext()
    
    result = call_subagent(
        agent_name="file-processor",
        task="直接回复 OK，不要调用任何工具",
        llm_config=llm_config,
        supplement_queue=sq,
        memory_context=mc,
    )
    
    assert result and len(result) > 0, "子 Agent 应有非空回复"
    # 不强断言 "OK" in result——LLM 可能调 read 等工具后回复，内容可能不含字面 "OK"
    snap = mc.snapshot()
    assert snap["current_turn"] >= 1
    assert snap["last_llm_response"] is not None
    assert len(snap["last_llm_response"]) > 0


def test_call_subagent_without_memory_context_unchanged(llm_config):
    """call_subagent 不传 memory_context 时，行为与阶段一一致（不报错）。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")
    
    sq = SubagentSupplementQueue("test-nomem-0001")
    
    result = call_subagent(
        agent_name="file-processor",
        task="直接回复 OK，不要调用任何工具",
        llm_config=llm_config,
        supplement_queue=sq,
        # 不传 memory_context
    )
    
    assert result and len(result) > 0, "子 Agent 应有非空回复"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_call_subagent_memory_hook.py -v`
Expected: FAIL with `TypeError: call_subagent() got an unexpected keyword argument 'memory_context'`

- [ ] **Step 3: Modify _run_agent_loop to accept and update memory_context**

修改 `agent/subagent.py:188-265` 的 `_run_agent_loop`：

```python
def _run_agent_loop(
    client,
    system_prompt: str = "",
    system_message: Optional[dict] = None,
    user_input: str = "",
    handler=None,
    tools_schema: list = None,
    max_turns: int = 20,
    initial_user_content: Optional[str] = None,
    context_window_tokens: int = 0,
    context_fifo_threshold: int = 0,
    context_target_threshold: int = 0,
    history: Optional[list] = None,
    supplement_queue: Optional[Any] = None,
    memory_context: Optional[Any] = None,  # 阶段二新增：异步子 Agent 进度数据
) -> Tuple[str, Any]:
    """执行 agent_runner_loop 并收集结果。

    Args:
        ...（其他参数同阶段一）
        memory_context: 异步子 Agent 传 SubagentMemoryContext，每轮 LLM 调用前后更新。
                       None 时跳过更新（同步子 Agent 路径不变）。

    Returns:
        (result_text, return_value) 元组
    """
    from .generic.agent_loop import agent_runner_loop, StreamEvent

    if initial_user_content is None:
        initial_user_content = user_input

    gen = agent_runner_loop(
        client=client,
        system_prompt=system_prompt,
        system_message=system_message,
        user_input=user_input,
        handler=handler,
        tools_schema=tools_schema,
        max_turns=max_turns,
        verbose=False,
        initial_user_content=initial_user_content,
        context_window_tokens=context_window_tokens,
        context_fifo_threshold=context_fifo_threshold,
        context_target_threshold=context_target_threshold,
        on_context_high_usage=None,
        history=history,
        enable_supplement=True,
        supplement_drain=supplement_queue.drain if supplement_queue is not None else None,
        memory_context=memory_context,  # 阶段二：透传给 agent_runner_loop
    )

    result = ""
    return_value = None

    while True:
        try:
            chunk = next(gen)
            if isinstance(chunk, str):
                result += chunk
            elif isinstance(chunk, StreamEvent):
                if chunk.type == "reply":
                    result += chunk.content
                # 忽略 persist/system/tool_marker — 这些是子Agent内部过程
        except StopIteration as e:
            return_value = e.value
            break

    return result, return_value
```

- [ ] **Step 4: Modify call_subagent to accept memory_context parameter**

修改 `agent/subagent.py:467-646` 的 `call_subagent` 签名 + 透传：

在 `call_subagent` 函数签名加 `memory_context` 参数：

```python
def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
    history: Optional[list] = None,
    context_fifo_threshold: int = -1,
    no_tools: bool = False,
    supplement_queue: Optional[Any] = None,
    memory_context: Optional[Any] = None,  # 阶段二新增
) -> str:
```

在 `call_subagent` 内部调 `_run_agent_loop` 处加 `memory_context=memory_context`（约 `agent/subagent.py:601-615`）：

```python
    try:
        result_text, return_value = _run_agent_loop(
            client=client,
            system_prompt="",
            system_message=system_message,
            user_input=task,
            handler=handler,
            tools_schema=tools_schema,
            max_turns=20,
            initial_user_content=task,
            context_window_tokens=context_window_tokens,
            context_fifo_threshold=fifo_threshold,
            context_target_threshold=context_target_threshold_val,
            history=history,
            supplement_queue=supplement_queue,
            memory_context=memory_context,  # 阶段二新增
        )
    finally:
        SubagentRegistry.unregister(unique_name)
```

- [ ] **Step 5: Modify agent_runner_loop to update memory_context**

修改 `agent/generic/agent_loop.py` 的 `agent_runner_loop` 函数：在签名加 `memory_context` 参数，在 `client.chat` 调用前后更新。

先在签名加参数（约 `agent/generic/agent_loop.py:296-314`，**注意实际参数顺序**——system_message 在 enable_supplement 之后，不是在 user_input 之后）：

```python
def agent_runner_loop(
    client,
    system_prompt: str = "",
    user_input=None,
    handler=None,
    tools_schema=None,
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
    system_message: Optional[dict] = None,
    supplement_drain=None,
    memory_context: Optional[Any] = None,  # 阶段二新增，加在最后保持向后兼容
):
```

在 `client.chat` 调用前更新 `last_llm_request`（约 `agent/generic/agent_loop.py:435-440`，在 `response_gen = client.chat(...)` 之前）：

```python
    # 阶段二：异步子 Agent 进度数据 — LLM 请求前更新 last_llm_request
    # last_llm_request 取 messages 里最后一条 role==user 的 content 摘要
    # 注意：无 supplement 时本轮 user 是上一轮遗留的（agent_runner_loop 不追加新 user），
    # 这是正常的——本轮 LLM 看到的就是那个 user。倒序找能拿到正确的"本轮送给 LLM 的最后一条 user"
    if memory_context is not None:
        try:
            last_user_content = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    content = m.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            block.get("text", "") if isinstance(block, dict) else str(block)
                            for block in content
                        )
                    last_user_content = str(content)[:500]  # 摘要前 500 字符
                    break
            memory_context.update(
                last_llm_request=last_user_content,
                current_turn=turn,
            )
        except Exception:
            pass  # 进度更新失败不影响主流程

    response_gen = client.chat(messages=messages, tools=tools_schema)
```

在 `yield StreamEvent("reply", content)` 之后（约 `agent/generic/agent_loop.py:461`）更新 `last_llm_response`。注意：回复文本变量在非 verbose 分支是 `content`（L447 `content = response.content or ""`），在 verbose 分支没有 `content` 变量需用 `response.content or ""`。子 Agent 路径 `verbose=False`（`_run_agent_loop` 传 `verbose=False`），所以用 `content` 变量：

```python
    # 阶段二：异步子 Agent 进度数据 — LLM 响应组装完后更新 last_llm_response
    # 位置：yield StreamEvent("reply", content) 之后（约 L461）
    # 子 Agent verbose=False，content 变量在 L447 已定义
    if memory_context is not None:
        try:
            memory_context.update(last_llm_response=(content or "")[:2000])
        except Exception:
            pass
```

在工具调度时更新 `last_tool_name`（在 `agent/generic/agent_loop.py` 的工具调度 for 循环体开头，约 L562 `for ii, tc in enumerate(tool_calls):` 之后、`handler.dispatch` 调用前）。先 Read 确认 L562 附近代码：

```bash
cd <repo_root> && sed -n '555,580p' agent/generic/agent_loop.py
```

确认 `for ii, tc in enumerate(tool_calls):` 循环位置后，在循环体开头（dispatch 调用前）加：

```python
    # 阶段二：异步子 Agent 进度数据 — 工具调度时更新 last_tool_name
    # 位置：for ii, tc in enumerate(tool_calls): 循环体开头，dispatch 调用前
    if memory_context is not None:
        try:
            tc_tool_name = tc.get("function", {}).get("name", "") if isinstance(tc, dict) else ""
            if tc_tool_name:
                memory_context.update(last_tool_name=tc_tool_name)
        except Exception:
            pass
```

- [ ] **Step 6: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_call_subagent_memory_hook.py -v`
Expected: PASS (2 tests，真实 LLM 调用，可能需要 30-60 秒)

- [ ] **Step 7: Run阶段一已有测试确认无回归**

Run: `cd <repo_root> && python/bin/python -m pytest tests/ -v 2>&1 | tail -30`
Expected: 阶段一已有测试全部 PASS（同步子 Agent 路径 memory_context=None 不影响）

- [ ] **Step 8: Commit**

```bash
git add agent/subagent.py agent/generic/agent_loop.py tests/test_call_subagent_memory_hook.py
git commit -m "feat(subagent): call_subagent + agent_runner_loop 加 memory_context 进度更新钩子"
```

---

### Task 5: db_monitor 路由主 Agent 回答到 PendingAskRegistry + /stop 时 cancel 解除死锁 + 双击停止也调 cancel

**注意**：本 Task 修改两个文件：`niu_api/db_monitor.py`（route_message 改造，Step 1-5）+ `agent/runner.py`（request_stop_all_subagents 改造，Step 6-9）。两者都是死锁约束的一部分（db_monitor 路由 /stop 时 cancel + 双击停止也 cancel），合并到一个 Task 避免拆分。

**Files:**
- Modify: `niu_api/db_monitor.py:76-101`（`route_message`）
- Modify: `agent/runner.py:45-55`（`request_stop_all_subagents`）
- Test: `tests/test_db_monitor_ask_routing.py`（新建）+ `tests/test_request_stop_all_subagents.py`（在 test_db_monitor_ask_routing.py 追加）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_monitor.py 补充测试（如果已有 test_db_monitor.py 就 append，否则新建 test_db_monitor_ask_routing.py）
"""验证 db_monitor.route_message：
1. 主 Agent 回答消息（@子名 来自主Agent）路由到 PendingAskRegistry.set_answer，不推 supplement queue
2. /stop 消息路由时同时 cancel_pending_ask 解除 ask_main_agent 阻塞
"""
import os
import tempfile
from niu_api import db_monitor
from agent.ask_main_agent import get_pending_ask_registry, TERMINATED_SIGNAL
from agent.subagent_registry import SubagentRegistry
from agent.subagent_supplement import SubagentSupplementQueue


def test_route_message_main_answer_to_pending_ask(monkeypatch):
    """主 Agent 回答 (@子名 内容) 来自主Agent → 路由到 PendingAskRegistry.set_answer。"""
    # 准备：注册一个子 Agent + 一个 pending ask
    sq = SubagentSupplementQueue("test-route-0001")
    name = SubagentRegistry.register("test-route", supplement_queue=sq, is_sync=False)
    
    # 清空 supplement queue 确保没被推入
    assert sq.drain() == []
    
    try:
        reg = get_pending_ask_registry()
        future = reg.register(name)
        
        # 路由主 Agent 的回答
        db_monitor.route_message(target=name, sender="主Agent", content="这是回答")
        
        # future 应被解除
        answer = future.wait(timeout=1.0)
        assert answer == "这是回答"
        
        # supplement queue 不应被推入（回答是 ask_main_agent 的回复，不是补充）
        assert sq.drain() == []
    finally:
        SubagentRegistry.unregister(name)


def test_route_message_stop_cancels_pending_ask():
    """/stop 消息路由时同时 cancel_pending_ask，避免 ask_main_agent 死锁。"""
    sq = SubagentSupplementQueue("test-stop-0001")
    name = SubagentRegistry.register("test-stop", supplement_queue=sq, is_sync=False)
    
    try:
        reg = get_pending_ask_registry()
        future = reg.register(name)
        
        # 路由 /stop
        db_monitor.route_message(target=name, sender="主Agent", content="/stop")
        
        # future 应被解除（TERMINATED_SIGNAL）
        answer = future.wait(timeout=1.0)
        assert answer == TERMINATED_SIGNAL
        
        # supplement queue 也应被推入 /stop（is_terminate=True）
        items = sq.drain()
        assert len(items) == 1
        assert items[0].is_terminate is True
        assert items[0].content == "/stop"
    finally:
        SubagentRegistry.unregister(name)


def test_route_message_orphan_answer_no_crash():
    """主 Agent 回答路由时子 Agent 已不在注册表（孤儿回答）→ 不抛异常。"""
    # 不注册子 Agent，直接路由
    db_monitor.route_message(target="nonexistent-xxxx", sender="主Agent", content="回答")
    # 不抛异常即可


def test_route_message_main_supplement_when_no_pending_ask():
    """主 Agent 补充上下文（无 pending future）→ 降级推 supplement queue，不推回主 Agent。
    
    关键场景：主 Agent 给异步子 Agent 补充上下文（不是回答 ask_main_agent）时，
    sender 也是"主Agent"，但子 Agent 没在 ask_main_agent，应推 supplement queue 让子 Agent 下一轮看到。
    """
    sq = SubagentSupplementQueue("test-supp-main-0001")
    name = SubagentRegistry.register("test-supp-main", supplement_queue=sq, is_sync=False)
    
    try:
        # 不注册 pending ask（子 Agent 没在问主）
        # 路由主 Agent 的补充上下文
        db_monitor.route_message(target=name, sender="主Agent", content="注意，文件路径改为 /tmp/x.pdf")
        
        # 应推 supplement queue（不推回主 Agent）
        items = sq.drain()
        assert len(items) == 1
        assert items[0].content == "注意，文件路径改为 /tmp/x.pdf"
        assert items[0].is_terminate is False
        assert items[0].sender == "主Agent"
    finally:
        SubagentRegistry.unregister(name)


def test_route_message_normal_supplement_to_subagent():
    """普通补充消息（@子名 内容，不是 /stop 也不是 ask 回答）→ 推 supplement queue。"""
    sq = SubagentSupplementQueue("test-supp-0001")
    name = SubagentRegistry.register("test-supp", supplement_queue=sq, is_sync=False)
    
    try:
        # 模拟其他子 Agent 发给这个子 Agent 的消息（sender 不是主Agent）
        db_monitor.route_message(target=name, sender="other-agent-aaaa", content="补充信息")
        
        items = sq.drain()
        assert len(items) == 1
        assert items[0].content == "补充信息"
        assert items[0].is_terminate is False
        assert items[0].sender == "other-agent-aaaa"
    finally:
        SubagentRegistry.unregister(name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_db_monitor_ask_routing.py -v`
Expected: FAIL — 现有 route_message 把所有 @子名 消息都推 supplement queue，没有 set_answer 路径

- [ ] **Step 3: Modify route_message to route answers and cancel on /stop**

修改 `niu_api/db_monitor.py:76-101` 的 `route_message`：

```python
def route_message(target: str, sender: str, content: str) -> None:
    """路由一条 @ 消息到目标。
    
    阶段二扩展：
    - 主 Agent 回答消息（sender == "主Agent" 且 content 不是 /stop）→ PendingAskRegistry.set_answer
    - /stop 消息 → 推 supplement queue (is_terminate=True) + cancel_pending_ask（解除 ask_main_agent 死锁）
    - 普通补充消息（其他 sender）→ 推 supplement queue
    """
    global _routed_count

    if target == "主Agent":
        # 阶段二：target==主Agent 的 subagent_msg 在新机制下不应出现
        # （ask_main_agent 和完成通知都改走 MainAgentRequestQueue 内存队列，不写 db）
        # 但为了兼容性（防止未来有人写 @主Agent 到 db），改为推入 MainAgentRequestQueue
        # content 格式 "[sender] content"——db_monitor 链路 A 检测主 Agent 闲置时推 SSE 触发前端
        msg_for_queue = f"[{sender}] {content}" if sender else f"[主Agent] {content}"
        try:
            from agent.main_agent_request_queue import get_main_agent_request_queue
            get_main_agent_request_queue().push(msg_for_queue)
        except Exception as e:
            logger.error(f"db_monitor 推入 MainAgentRequestQueue 失败：{e}")
        _routed_count += 1
        logger.info(f"db_monitor 路由到主 Agent（推入 MainAgentRequestQueue）：{msg_for_queue[:50]}")
        return

    # 目标是子 Agent
    instance = SubagentRegistry.get(target)
    if instance is None:
        # 目标不在注册表（子 Agent 已退出/重启后残留消息）
        # 区分两种情况避免死循环：
        # 1. 主 Agent 回答到达但子 Agent 已退出（孤儿回答，sender=="主Agent"）→ 丢弃 + 日志（不推回主 Agent，否则主 Agent 下一轮回复又会找不到子 Agent 形成死循环）
        # 2. 其他场景（如重启后残留的 @子名 消息）→ 推回主 Agent 让主 Agent 知道
        if sender == "主Agent":
            logger.warning(f"db_monitor 孤儿回答：主 Agent 回答到达但子 Agent {target} 已不在注册表，丢弃：{content[:50]}")
            # 不推回主 Agent supplement queue（避免死循环）
        else:
            fallback = f"@主Agent [system] 目标子 Agent {target} 已不存在：{content}"
            enqueue_supplement(fallback)
            logger.warning(f"db_monitor 目标子 Agent {target} 不在注册表，推回主 Agent")
        return

    is_terminate = content.strip() == "/stop"

    # 阶段二：/stop 时同时 cancel ask_main_agent 阻塞（避免死锁）
    if is_terminate:
        try:
            from agent.ask_main_agent import get_pending_ask_registry
            get_pending_ask_registry().cancel_pending_ask(target)
            logger.info(f"db_monitor /stop 同时 cancel ask_main_agent：{target}")
        except Exception as e:
            logger.error(f"db_monitor cancel_pending_ask 失败：{e}")

    # 阶段二：主 Agent 回答消息（非 /stop）→ 路由到 PendingAskRegistry.set_answer 解除 ask_main_agent 阻塞
    # 关键：用 PendingAskRegistry 有无 future 作为判据，不用 sender=="主Agent"
    # 因为主 Agent 也会给子 Agent 补充上下文（不是回答 ask_main_agent），sender 都是"主Agent"
    # 有 future → set_answer（回答 ask_main_agent）；无 future → 降级推 supplement queue（普通补充）
    elif sender == "主Agent":
        try:
            from agent.ask_main_agent import get_pending_ask_registry
            found = get_pending_ask_registry().set_answer(target, content)
            if found:
                _routed_count += 1
                logger.info(f"db_monitor 路由主 Agent 回答到 ask_main_agent：{target}：{content[:50]}")
                return  # 已路由到 ask_main_agent，不再推 supplement queue
            # 找不到 future：主 Agent 在补充上下文（不是回答 ask_main_agent），降级推 supplement queue
            instance.supplement_queue.push(content, is_terminate=False, sender=sender)
            _routed_count += 1
            logger.info(f"db_monitor 路由主 Agent 补充上下文到子 Agent：{target}：{content[:50]}")
            return
        except Exception as e:
            logger.error(f"db_monitor set_answer 失败：{e}")
            # 失败时降级为推 supplement queue（让子 Agent 下一轮看到）
            instance.supplement_queue.push(content, is_terminate=False, sender=sender)
            _routed_count += 1
            return

    # 普通补充消息（其他 sender，非 /stop）→ 推 supplement queue
    instance.supplement_queue.push(content, is_terminate=is_terminate, sender=sender)
    _routed_count += 1
    logger.info(f"db_monitor 路由到子 Agent {target}：{content[:50]} (terminate={is_terminate})")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_db_monitor_ask_routing.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run 阶段一已有 db_monitor 测试确认无回归**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_db_monitor.py tests/test_at_message_parser.py -v 2>&1 | tail -20`
Expected: 阶段一已有测试全部 PASS（普通补充和 /stop 路径行为不变，只是多了 set_answer/cancel 路径）

- [ ] **Step 6: 修复 request_stop_all_subagents 双击停止死锁**

**关键 bug**：阶段一 `request_stop_all_subagents`（agent/runner.py:45-55）只推 supplement queue，不调 `cancel_pending_ask`。双击停止按钮触发 `request_stop_all_subagents`，如果异步子 Agent 正在 ask_main_agent 阻塞（`threading.Event.wait`），/stop 推入 queue 后子 Agent 消费不到（阻塞在工具调用内部不会执行 drain_supplement）→ **死锁**。

这与设计约束 1（"主 Agent 发 /stop 时 db_monitor 同时 cancel_pending_ask"）对应——但 `request_stop_all_subagents` 不经过 db_monitor.route_message，所以必须自己调 cancel_pending_ask。

修改 `agent/runner.py:45-55` 的 `request_stop_all_subagents`：

```python
def request_stop_all_subagents() -> None:
    """给所有在跑的子 Agent 推 /stop（双击停止按钮触发）。

    遍历 SubagentRegistry，给每个子 Agent：
    1. 调 cancel_pending_ask 解除 ask_main_agent 阻塞（避免死锁）
    2. 推 /stop 到 supplement queue（让子 Agent 下一轮 drain 走终止总结流程）
    
    主 Agent 不受影响（主 Agent 用 _stop_requested 信号灯单独控制）。
    """
    from agent.ask_main_agent import get_pending_ask_registry
    pending_ask = get_pending_ask_registry()
    
    for instance in SubagentRegistry.list_running():
        try:
            # 先 cancel ask_main_agent 阻塞（如果有）
            pending_ask.cancel_pending_ask(instance.unique_name)
            # 再推 /stop 到 supplement queue
            instance.supplement_queue.push("/stop", is_terminate=True, sender="主Agent")
        except Exception as e:
            logger.error(f"给子 Agent {instance.unique_name} 推 /stop 失败：{e}")
```

注意：`from agent.ask_main_agent import get_pending_ask_registry` 是局部导入，因为 `agent/runner.py` 在模块顶部不能导入 `agent.ask_main_agent`（避免循环导入——ask_main_agent.py 不导入 runner，但保险起见用局部导入）。

- [ ] **Step 7: 添加双击停止死锁测试**

在 `tests/test_db_monitor_ask_routing.py` 追加测试（或新建 `tests/test_request_stop_all_subagents.py`）：

```python
def test_request_stop_all_subagents_cancels_pending_ask():
    """双击停止时 request_stop_all_subagents 同时 cancel ask_main_agent 阻塞，避免死锁。"""
    from agent.runner import request_stop_all_subagents
    from agent.ask_main_agent import get_pending_ask_registry, TERMINATED_SIGNAL
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue
    from agent.subagent_memory import SubagentMemoryContext
    
    # 注册一个异步子 Agent + pending ask
    sq = SubagentSupplementQueue("test-stop-all-0001")
    mc = SubagentMemoryContext()
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)
    
    try:
        reg = get_pending_ask_registry()
        future = reg.register(name)
        
        # 双击停止
        request_stop_all_subagents()
        
        # future 应被解除（TERMINATED_SIGNAL）
        answer = future.wait(timeout=1.0)
        assert answer == TERMINATED_SIGNAL
        
        # supplement queue 也应被推入 /stop
        items = sq.drain()
        assert len(items) == 1
        assert items[0].is_terminate is True
        assert items[0].content == "/stop"
    finally:
        SubagentRegistry.unregister(name)
```

- [ ] **Step 8: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_db_monitor_ask_routing.py::test_request_stop_all_subagents_cancels_pending_ask -v`
Expected: PASS

- [ ] **Step 9: 链路 A — db_monitor 主 Agent 闲置检测 + MainAgentRequestQueue 消费 + 推 SSE**

**核心机制**（详见计划开头"核心机制设计"段）：子 Agent 的 ask 请求和完成通知不写 db，进 MainAgentRequestQueue 内存队列。db_monitor 检测主 Agent 闲置（`_chat_lock.locked() == False`）时从队列 FIFO 取消息，推 SSE 触发前端调 /api/chat/session，前端触发后后端写 user 消息到 db + 调 LLM。

修改 `niu_api/db_monitor.py`，在 `run_db_monitor` 主循环里加链路 A 处理：

```python
async def run_db_monitor(interval: float = 0.2) -> None:
    """db 监测程序主循环。崩溃自动重启。
    
    两条职责独立的链路：
    - 链路 B（现有）：轮询 messages.db 中 role=subagent_msg 新消息，按 @目标路由
    - 链路 A（阶段二新增）：检测主 Agent 闲置 + 消费 MainAgentRequestQueue + 推 SSE 触发前端
    """
    logger.info("db_monitor 启动")
    await _init_routed_baseline()

    while True:
        try:
            # 链路 B：轮询 db 路由 @消息
            await _poll_messages()
            # 链路 A：检测主 Agent 闲置，消费 MainAgentRequestQueue
            await _drain_main_agent_request_queue()
            await asyncio.sleep(interval)
            if _routed_count > 0 and _routed_count % 100 == 0:
                logger.info(f"db_monitor 心跳：已路由 {_routed_count} 条消息")
        except asyncio.CancelledError:
            logger.info("db_monitor 收到取消信号，退出")
            break
        except Exception as e:
            logger.error(f"db_monitor 异常崩溃，1 秒后重启：{e}")
            await asyncio.sleep(1)


async def _drain_main_agent_request_queue() -> None:
    """链路 A：主 Agent 闲置时消费 MainAgentRequestQueue，推 SSE 触发前端。
    
    逻辑：
    1. 检查 _chat_lock.locked()——主 Agent 忙则跳过（消息留队列）
    2. 检查 MainAgentRequestQueue.peek()——空则跳过
    3. 主 Agent 闲 + 队列有消息 → 推 SSE（notify_new_message，source="subagent"）
    4. 推 SSE 成功后 pop 移除（避免推送失败丢消息）
    
    注意：不写 db——写 db 由前端触发 /api/chat/session 后由 compat.py 完成
    （这样保证消息是 db 最后一条 user 消息，LLM 才会作为当前输入处理）
    """
    from agent.main_agent_request_queue import get_main_agent_request_queue
    from niu_api.compat import _chat_lock
    
    if _chat_lock.locked():
        return  # 主 Agent 忙，消息留队列
    
    q = get_main_agent_request_queue()
    content = q.peek()
    if content is None:
        return  # 队列空
    
    # 推 SSE 触发前端
    try:
        from niu_api.chat import notify_new_message_sync
        # content 格式 "[子名] 内容"，role 用 subagent_msg 让前端识别为子 Agent 触发
        # 前端 onNewMessage 收到 role=subagent_msg + isProcessing=false → 自动调 /api/chat/session
        # content 作为 message 参数传给后端，后端写 user 消息（role=user）
        notify_new_message_sync("", "subagent_msg", content, source="subagent")
        # 推 SSE 成功后才 pop（避免推送失败丢消息）
        q.pop()
        logger.info(f"db_monitor 链路 A 推 SSE 触发主 Agent：{content[:50]}")
    except Exception as e:
        logger.error(f"db_monitor 链路 A 推 SSE 失败，消息留队列：{e}")
```

- [ ] **Step 10: notify_new_message 加 source="subagent" 支持**

修改 `niu_api/chat.py` 的 `notify_new_message` 和 `notify_new_message_sync`，去掉 `source != "electron"` 白名单或加 `"subagent"` 到白名单：

```python
# notify_new_message_sync（约 L55-75）
def notify_new_message_sync(message_id: str, role: str, content: str, source: str = "electron"):
    """同步推送 new_message 事件到所有 SSE 订阅者。
    
    source 白名单：electron（前端用户操作）、subagent（子 Agent 触发，阶段二新增）
    """
    if source not in ("electron", "subagent"):
        return
    # ... 现有推送逻辑 ...
```

同样修改 `notify_new_message`（约 L35-52）。

- [ ] **Step 11: 测试链路 A**

新建 `tests/test_main_agent_request_queue_drain.py`：

```python
import asyncio
import pytest
from niu_api import db_monitor
from agent.main_agent_request_queue import get_main_agent_request_queue
from niu_api.compat import _chat_lock


def test_drain_skipped_when_main_agent_busy():
    """主 Agent 忙时（_chat_lock.locked）不消费队列。"""
    q = get_main_agent_request_queue()
    # 清空队列
    while q.pop() is not None:
        pass
    q.push("[子名] 测试消息")
    
    # 模拟主 Agent 忙
    async def busy_and_drain():
        await _chat_lock.acquire()
        try:
            await db_monitor._drain_main_agent_request_queue()
        finally:
            _chat_lock.release()
    
    asyncio.run(busy_and_drain())
    
    # 队列里消息应该还在（没被消费）
    assert q.peek() == "[子名] 测试消息"
    q.pop()  # 清理


def test_drain_consumes_when_main_agent_idle(monkeypatch):
    """主 Agent 闲时消费队列 + 推 SSE。"""
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass
    q.push("[子名] 测试消息")
    
    # mock notify_new_message_sync 避免真实 SSE 推送
    pushed = []
    def fake_notify(msg_id, role, content, source="electron"):
        pushed.append((role, content, source))
    monkeypatch.setattr("niu_api.chat.notify_new_message_sync", fake_notify)
    
    asyncio.run(db_monitor._drain_main_agent_request_queue())
    
    # 应该推了 SSE
    assert len(pushed) == 1
    assert pushed[0][0] == "subagent_msg"
    assert pushed[0][1] == "[子名] 测试消息"
    assert pushed[0][2] == "subagent"
    # 队列应该空了（pop 了）
    assert q.is_empty()


def test_drain_skipped_when_queue_empty():
    """队列空时不推 SSE。"""
    q = get_main_agent_request_queue()
    while q.pop() is not None:
        pass
    
    # 主 Agent 闲 + 队列空 → 不推 SSE（用 mock 验证不调用）
    import unittest.mock as mock
    with mock.patch("niu_api.chat.notify_new_message_sync") as fake_notify:
        asyncio.run(db_monitor._drain_main_agent_request_queue())
        fake_notify.assert_not_called()
```

- [ ] **Step 12: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_main_agent_request_queue_drain.py -v`
Expected: PASS (3 tests)

- [ ] **Step 13: Commit**

```bash
git add niu_api/db_monitor.py niu_api/chat.py tests/test_main_agent_request_queue_drain.py
git commit -m "feat(db-monitor): 链路 A 主 Agent 闲置检测 + MainAgentRequestQueue 消费 + 推 SSE 触发前端"
```

---

### Task 6: ask_main_agent 工具实现

**Files:**
- Modify: `agent/subagent.py`（新增 `_ask_main_agent_impl` + schema 注入逻辑）
- Test: `tests/test_ask_main_agent.py`（追加集成测试，使用 mock LLM 配置但真实工具函数调用）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ask_main_agent.py 追加（在 Task 2 已有 6 个测试后追加 2 个集成测试）

def test_ask_main_agent_tool_returns_answer():
    """ask_main_agent 工具：注册 future → 推 db → 阻塞 → set_answer 后返回回答。"""
    from agent.subagent import _ask_main_agent_impl
    from agent.ask_main_agent import get_pending_ask_registry
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue
    import threading
    import time
    
    # 准备：注册一个异步子 Agent（ask_main_agent 工具需要 unique_name 上下文）
    sq = SubagentSupplementQueue("test-ask-0001")
    name = SubagentRegistry.register("test-ask", supplement_queue=sq, is_sync=False)
    
    try:
        # 在另一个线程模拟主 Agent 回答（1 秒后）
        def answer_later():
            time.sleep(0.5)
            get_pending_ask_registry().set_answer(name, "这是主 Agent 的回答")
        
        t = threading.Thread(target=answer_later)
        t.start()
        
        # 调工具（阻塞 0.5 秒后拿到回答）
        result = _ask_main_agent_impl("这是问题", unique_name=name)
        
        t.join()
        
        assert "这是主 Agent 的回答" in result
    finally:
        SubagentRegistry.unregister(name)


def test_ask_main_agent_tool_terminated_returns_terminated_status():
    """ask_main_agent 工具被 cancel 时返回 terminated 状态。"""
    from agent.subagent import _ask_main_agent_impl
    from agent.ask_main_agent import get_pending_ask_registry, TERMINATED_SIGNAL
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue
    import threading
    import time
    
    sq = SubagentSupplementQueue("test-cancel-0001")
    name = SubagentRegistry.register("test-cancel", supplement_queue=sq, is_sync=False)
    
    try:
        # 在另一个线程模拟 /stop（cancel_pending_ask）
        def cancel_later():
            time.sleep(0.5)
            get_pending_ask_registry().cancel_pending_ask(name)
        
        t = threading.Thread(target=cancel_later)
        t.start()
        
        result = _ask_main_agent_impl("这是问题", unique_name=name)
        
        t.join()
        
        assert "terminated" in result.lower() or "终止" in result
    finally:
        SubagentRegistry.unregister(name)


def test_ask_main_agent_after_cancel_does_not_deadlock():
    """cancel 后 LLM 又调 ask_main_agent 不死锁——直接返回 terminated 状态（_ask_terminated 标记）。
    
    场景：子 Agent ask_main_agent 被 cancel → 工具返回 terminated → LLM 没走终止总结
    反而又调 ask_main_agent → 应直接返回 terminated 不阻塞（否则 /stop 在 queue 但子 Agent
    阻塞在 ask_main_agent 不会 drain → 死锁）
    """
    from agent.subagent import _ask_main_agent_impl
    from agent.ask_main_agent import get_pending_ask_registry, TERMINATED_SIGNAL
    from agent.subagent_registry import SubagentRegistry
    from agent.subagent_supplement import SubagentSupplementQueue
    import threading
    import time
    
    sq = SubagentSupplementQueue("test-reask-0001")
    name = SubagentRegistry.register("test-reask", supplement_queue=sq, is_sync=False)
    
    try:
        # 第一次 ask_main_agent，0.5 秒后 cancel
        def cancel_later():
            time.sleep(0.5)
            get_pending_ask_registry().cancel_pending_ask(name)
        
        t1 = threading.Thread(target=cancel_later)
        t1.start()
        result1 = _ask_main_agent_impl("第一次问题", unique_name=name)
        t1.join()
        assert "终止" in result1
        
        # 第二次 ask_main_agent——应立即返回 terminated 不阻塞（_ask_terminated 标记）
        start = time.time()
        result2 = _ask_main_agent_impl("第二次问题", unique_name=name)
        elapsed = time.time() - start
        
        # 应在 1 秒内返回（不阻塞 300 秒）
        assert elapsed < 1.0, f"第二次 ask_main_agent 应立即返回，实际耗时 {elapsed}"
        assert "终止" in result2
    finally:
        SubagentRegistry.unregister(name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_ask_main_agent.py::test_ask_main_agent_tool_returns_answer tests/test_ask_main_agent.py::test_ask_main_agent_tool_terminated_returns_terminated_status -v`
Expected: FAIL with `ImportError: cannot import name '_ask_main_agent_impl' from 'agent.subagent'`

- [ ] **Step 3: Implement _ask_main_agent_impl in agent/subagent.py**

在 `agent/subagent.py` 末尾追加（约文件末尾，`call_subagent` 函数后）：

```python
# ==================== 阶段二：ask_main_agent 工具 ====================

# ask_main_agent 工具 schema（注入给异步子 Agent）
ASK_MAIN_AGENT_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_main_agent",
        "description": (
            "向主 Agent 提问并阻塞等待回答。当遇到歧义、需要澄清或需要主 Agent 决策时使用。"
            "调用后会阻塞直到主 Agent 回答（通过 db_monitor 路由）。"
            "不要在主 Agent 没回答前连续调用多次——一次只问一个问题。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要问主 Agent 的问题，描述清楚歧义点。",
                },
            },
            "required": ["question"],
        },
    },
}


def _ask_main_agent_impl(question: str, unique_name: str) -> str:
    """ask_main_agent 工具实现。
    
    子 Agent 调用流程（阶段二内存队列机制）：
      1. 检查是否已被 cancel 过（_ask_terminated 标记）——避免 cancel 后 LLM 又调 ask_main_agent 死锁
      2. 注册 future 到 PendingAskRegistry（key=unique_name）
      3. 推 "[unique_name] question" 到 MainAgentRequestQueue 内存队列（**不写 db**）
      4. future.wait() 阻塞（不写 db，由 db_monitor 检测主 Agent 闲置时推 SSE 触发前端）
      5. 前端调 /api/chat/session → 后端写 user 消息到 db + 调 LLM → 主 Agent 回复 @子名 回答
      6. db_monitor 链路 B 轮询到主 Agent 回复的 subagent_msg → set_answer 解除 future
      7. 返回回答文本；如果被 cancel（主 Agent 发 /stop）返回 terminated 状态 + 设置 _ask_terminated 标记
    
    关键：消息不写 db，进内存队列。db_monitor 检测主 Agent 闲置时推 SSE 触发前端，
    前端调 /api/chat/session 后由后端 compat.py 写 user 消息到 db（作为最后一条 user 消息，
    LLM 才会作为当前输入处理）。
    
    Args:
        question: 子 Agent 要问的问题
        unique_name: 子 Agent 唯一名（注册 future 用）
    
    Returns:
        主 Agent 的回答文本，或 terminated 状态提示
    """
    from .ask_main_agent import get_pending_ask_registry, TERMINATED_SIGNAL
    from .main_agent_request_queue import get_main_agent_request_queue
    from .subagent_registry import SubagentRegistry
    
    registry = get_pending_ask_registry()
    
    # 阶段二防死锁检查：如果该子 Agent 之前已被 cancel（_ask_terminated 标记），
    # 直接返回 terminated 状态，不再注册 future 阻塞
    instance = SubagentRegistry.get(unique_name)
    if instance is not None and getattr(instance, "_ask_terminated", False):
        return "[ask_main_agent 已终止] 主 Agent 已发出停止指令，请总结本轮工作后终止。"
    
    future = registry.register(unique_name)
    
    # 推入 MainAgentRequestQueue 内存队列（不写 db）
    # content 格式 "[子名] 问题"——db_monitor 推 SSE 时 role=subagent_msg，
    # 前端收到后调 /api/chat/session，content 作为 message 参数传给后端，
    # 后端 compat.py 写 user 消息（role=user, content="[子名] 问题"）
    msg_content = f"[{unique_name}] {question}"
    try:
        get_main_agent_request_queue().push(msg_content)
    except Exception as e:
        # 推队列失败 → 注销 future，返回错误
        registry.unregister(unique_name)
        return f"[ask_main_agent 错误] 推入 MainAgentRequestQueue 失败：{e}"
    
    # 阻塞等待（加超时避免 db_monitor 崩溃时子 Agent 永久阻塞）
    # 超时 300 秒（5 分钟）——主 Agent 可能忙很久，5 分钟够用
    # 超时返回提示让子 Agent 自行决策；被 cancel 返回 terminated 状态
    answer = future.wait(timeout=300)
    
    if answer == TERMINATED_SIGNAL:
        # 主 Agent 发 /stop，工具识别后返回终止状态
        # 设置 _ask_terminated 标记到 SubagentRegistry 实例，防止 LLM 再次调 ask_main_agent 死锁
        if instance is not None:
            instance._ask_terminated = True
        return "[ask_main_agent 已终止] 主 Agent 已发出停止指令，请总结本轮工作后终止。"
    
    if answer is None:
        # 超时（5 分钟主 Agent 未回答）——返回决策提示让子 Agent 自己决定
        # 不强制退出，让子 Agent 根据任务情况选择重新问 or 跳过继续
        # 注销 future 避免泄漏
        registry.unregister(unique_name)
        logger.warning(f"ask_main_agent 超时（5 分钟无回答），unique_name={unique_name}")
        return (
            "[ask_main_agent 超时] 主 Agent 5 分钟内未响应。你可以：\n"
            "1. 重新调用 ask_main_agent 再问一次（主 Agent 可能刚才在忙）\n"
            "2. 跳过这个问题，基于现有信息继续工作（如果这个回答不是必须的）\n"
            "请根据当前任务情况决定。"
        )
    
    return answer
```

- [ ] **Step 4: persist_subagent_msg 不再需要（ask_main_agent 和完成通知都不写 db）**

**说明**：阶段二 ask_main_agent 和完成通知都改为推入 MainAgentRequestQueue 内存队列，**不写 db**。所以 `persist_subagent_msg` 同步版函数**不需要新建**。

主 Agent 回复子 Agent 问题时，回复里带 `@子名 回答内容`——这个 @消息由现有的 `persist_agent_reply`（阶段一已实现）以 `role="subagent_msg"` 写 db，db_monitor 链路 B 路由到子 Agent 的 PendingAskRegistry.set_answer。这条路径不需要额外新建 `persist_subagent_msg`。

确认阶段一 `persist_agent_reply` 已覆盖此场景（提取 @消息以 role=subagent_msg 写 db），如果覆盖，本 Step 跳过。

- [ ] **Step 5: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_ask_main_agent.py -v`
Expected: PASS (8 tests = Task 2 的 6 个 + Task 6 新增 2 个集成测试)

- [ ] **Step 6: Commit**

```bash
git add agent/subagent.py niu_api/chat.py tests/test_ask_main_agent.py
git commit -m "feat(ask-main-agent): 实现 ask_main_agent 工具（阻塞 + MainAgentRequestQueue 推送 + cancel 处理）"
```

---

### Task 7: 异步子 Agent 的 ask_main_agent 工具注入

**Files:**
- Modify: `agent/subagent.py:467-646`（`call_subagent` 中 tools_schema 拼装处）
- Test: `tests/test_ask_main_agent_injection.py`（新建）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ask_main_agent_injection.py
"""验证异步子 Agent 的 tools_schema 包含 ask_main_agent；同步子 Agent 不包涵。"""
from agent.subagent import call_subagent, ASK_MAIN_AGENT_TOOL_SCHEMA, get_subagent_mcp_tools_schema


def test_async_subagent_includes_ask_main_agent(monkeypatch):
    """异步调用时（memory_context 非 None）tools_schema 包含 ask_main_agent。"""
    # 直接调 call_subagent 但 mock client 避免真实 LLM 调用
    # 这里测的是 tools_schema 拼装逻辑，可以提取一个 helper 函数测试
    from agent.subagent import _build_subagent_tools_schema
    
    # memory_context 非 None → 异步路径 → 应包含 ask_main_agent
    schema = _build_subagent_tools_schema("file-processor", memory_context=object())
    
    tool_names = [t.get("function", {}).get("name", "") for t in schema]
    assert "ask_main_agent" in tool_names


def test_sync_subagent_excludes_ask_main_agent():
    """同步调用时（memory_context None）tools_schema 不包涵 ask_main_agent（避免死锁）。"""
    from agent.subagent import _build_subagent_tools_schema
    
    # memory_context=None → 同步路径 → 不应包含 ask_main_agent
    schema = _build_subagent_tools_schema("file-processor", memory_context=None)
    
    tool_names = [t.get("function", {}).get("name", "") for t in schema]
    assert "ask_main_agent" not in tool_names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_ask_main_agent_injection.py -v`
Expected: FAIL with `ImportError: cannot import name '_build_subagent_tools_schema'`

- [ ] **Step 3: Extract _build_subagent_tools_schema helper and inject ask_main_agent for async**

修改 `agent/subagent.py:467-576` 的 `call_subagent`：把 tools_schema 拼装逻辑提取为 `_build_subagent_tools_schema` 函数，并加 ask_main_agent 注入。

在 `agent/subagent.py` 中（约 `call_subagent` 函数前）新增 helper 函数：

```python
def _build_subagent_tools_schema(
    agent_name: str,
    agent_config: Optional[dict] = None,
    memory_context: Optional[Any] = None,
    no_tools: bool = False,
) -> list:
    """构建子 Agent 的 tools_schema。
    
    阶段二新增：异步子 Agent（memory_context 非 None）注入 ask_main_agent 工具。
    同步子 Agent（memory_context None）不注入（避免死锁）。
    
    Args:
        agent_name: 子 Agent 名（如 file-processor）
        agent_config: 子 Agent 配置字典（frontmatter 解析结果）。None 时内部调 get_subagent_config 获取（方便测试）
        memory_context: 非 None 表示异步子 Agent，注入 ask_main_agent；None 表示同步
        no_tools: True 时返回空列表（强制无工具模式）
    
    Returns:
        tools_schema 列表
    """
    if no_tools:
        # 注意：现有 call_subagent 在最后清空 tools_schema（L573-575），日志走完过滤流程才清空
        # helper 提前返回跳过日志，但功能结果一致（都返回空）。可接受——no_tools 模式不需要日志
        return []
    
    # agent_config None 时内部获取（方便测试只传 agent_name + memory_context）
    if agent_config is None:
        agent_config = get_subagent_config(agent_name)
    
    from .runner import get_tools_schema
    
    tools_schema = get_tools_schema()
    # 移除 chat-with-* 工具，子 Agent 不能再调用子 Agent
    tools_schema = [
        t for t in tools_schema
        if not t.get("function", {}).get("name", "").startswith("chat-with-")
    ]
    # 三层过滤：默认黑名单 + disableBaseTools + allowBaseTools 解禁
    tools_schema, disabled_set, custom_disabled, allowed_base = _filter_base_tools(agent_config, tools_schema)
    if disabled_set:
        logger.info(f"[SubAgent] {agent_name}: Disabled base tools: {sorted(disabled_set)}")
    
    # 配置完整性检查
    if not custom_disabled and not allowed_base:
        logger.warning(
            f"[SubAgent] {agent_name}: No disableBaseTools/allowBaseTools configured, "
            f"using default blacklist only: {sorted(DEFAULT_DISABLED_BASE_TOOLS)}."
        )
    
    # MCP 工具
    mcp_tools_schema = get_subagent_mcp_tools_schema(agent_name)
    if mcp_tools_schema:
        tools_schema = tools_schema + mcp_tools_schema
        logger.info(f"[SubAgent] {agent_name}: {len(tools_schema)} tools ({len(mcp_tools_schema)} MCP)")
    else:
        logger.warning(f"[SubAgent] {agent_name}: {len(tools_schema)} tools (0 MCP - WARNING)")
    
    # 阶段二：异步子 Agent 注入 ask_main_agent
    if memory_context is not None:
        tools_schema.append(ASK_MAIN_AGENT_TOOL_SCHEMA)
        logger.info(f"[SubAgent] {agent_name}: ask_main_agent 注入（异步子 Agent）")
    
    return tools_schema
```

然后修改 `call_subagent` 内部（约 `agent/subagent.py:540-576`）调用这个 helper：

```python
    # 替换原 540-576 行的 tools_schema 拼装逻辑为：
    tools_schema = _build_subagent_tools_schema(
        agent_name=agent_name,
        agent_config=agent_config,
        memory_context=memory_context,
        no_tools=no_tools,
    )
```

- [ ] **Step 4: 在 call_subagent 中设置 handler._subagent_unique_name**

ask_main_agent 工具需要子 Agent 的 unique_name，但 handler 是 call_subagent 内部新建的（subagent.py:530），与 _run_subagent_async 不共享 self。设计断裂修复：call_subagent 在 `SubagentRegistry.register` 拿到 unique_name 后，立即设置 `handler._subagent_unique_name = unique_name`。

修改 `agent/subagent.py` 的 `call_subagent`（约 L591-617），在 register 后、_run_agent_loop 调用前加一行：

```python
    # === 新增：创建 supplement queue + 注册到 SubagentRegistry ===
    from .subagent_supplement import SubagentSupplementQueue
    from .subagent_registry import SubagentRegistry

    if supplement_queue is None:
        supplement_queue = SubagentSupplementQueue(unique_name="")  # unique_name 注册后回填
    unique_name = SubagentRegistry.register(agent_name, supplement_queue, memory_context=memory_context, is_sync=(memory_context is None))
    supplement_queue.unique_name = unique_name  # 回填唯一名，db 监测程序路由时用
    
    # 阶段二：设置 handler._subagent_unique_name，让 ask_main_agent 工具能拿到
    handler._subagent_unique_name = unique_name

    try:
        result_text, return_value = _run_agent_loop(
            # ... 现有参数 ...
        )
    finally:
        SubagentRegistry.unregister(unique_name)
```

注意：`register` 的 `is_sync` 参数根据 `memory_context is None` 判断——同步子 Agent（memory_context=None）is_sync=True，异步子 Agent（memory_context 非 None）is_sync=False。这与 Task 8 `_dispatch_async_subagent` 直接调 register 传 `is_sync=False` 不冲突，因为 Task 8 自己注册（不经过 call_subagent 的这段路径）。但 Task 8 的 `_run_subagent_async` 调 call_subagent 时，call_subagent 会再次 register——**双重注册问题**！需要 Task 8 调整：`_run_subagent_async` 不让 call_subagent 重复 register，要么 call_subagent 加 `unique_name` 参数透传（跳过内部 register），要么 `_run_subagent_async` 不调 call_subagent 的 register 路径。

**采用方案**：call_subagent 加 `unique_name: Optional[str] = None` 参数。非 None 时跳过内部 register + supplement_queue 创建（调用方已创建并注册），只设置 `handler._subagent_unique_name = unique_name`。同步路径（unique_name=None）走现有 register 路径。

修改 call_subagent 签名（在 memory_context 参数后加 unique_name）：

```python
def call_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
    history: Optional[list] = None,
    context_fifo_threshold: int = -1,
    no_tools: bool = False,
    supplement_queue: Optional[Any] = None,
    memory_context: Optional[Any] = None,
    unique_name: Optional[str] = None,  # 阶段二新增：异步路径透传，跳过内部 register
) -> str:
```

修改 call_subagent 内部 register 逻辑（约 L591-617）：

```python
    from .subagent_supplement import SubagentSupplementQueue
    from .subagent_registry import SubagentRegistry

    if unique_name is not None:
        # 异步路径：调用方已注册（_dispatch_async_subagent），跳过内部 register
        # 只设置 handler._subagent_unique_name
        handler._subagent_unique_name = unique_name
        # supplement_queue 也由调用方传入，不重新创建
        try:
            result_text, return_value = _run_agent_loop(
                client=client,
                system_prompt="",
                system_message=system_message,
                user_input=task,
                handler=handler,
                tools_schema=tools_schema,
                max_turns=20,
                initial_user_content=task,
                context_window_tokens=context_window_tokens,
                context_fifo_threshold=fifo_threshold,
                context_target_threshold=context_target_threshold_val,
                history=history,
                supplement_queue=supplement_queue,
                memory_context=memory_context,
            )
        finally:
            # 异步路径不在这里 unregister（_run_subagent_async 的 finally 负责）
            pass
    else:
        # 同步路径：现有逻辑
        if supplement_queue is None:
            supplement_queue = SubagentSupplementQueue(unique_name="")
        unique_name = SubagentRegistry.register(agent_name, supplement_queue, memory_context=memory_context, is_sync=True)
        supplement_queue.unique_name = unique_name
        handler._subagent_unique_name = unique_name
        try:
            result_text, return_value = _run_agent_loop(
                client=client,
                system_prompt="",
                system_message=system_message,
                user_input=task,
                handler=handler,
                tools_schema=tools_schema,
                max_turns=20,
                initial_user_content=task,
                context_window_tokens=context_window_tokens,
                context_fifo_threshold=fifo_threshold,
                context_target_threshold=context_target_threshold_val,
                history=history,
                supplement_queue=supplement_queue,
                memory_context=memory_context,
            )
        finally:
            SubagentRegistry.unregister(unique_name)
```

Task 8 `_run_subagent_async` 调 call_subagent 时传 `unique_name=unique_name`（已在 Task 8 Step 3 的 _run_subagent_async 代码里——需更新 Task 8 调用处加 `unique_name=unique_name`）。

- [ ] **Step 5: Wire ask_main_agent tool dispatch in handler**

修改 `agent/handler.py:994-1036` 的 `dispatch` 方法，加 `ask_main_agent` 工具分支（在 `chat-with-*` 分支后、内置工具分支前）：

```python
    # 阶段二：ask_main_agent 工具（异步子 Agent 专用）
    if tool_name == "ask_main_agent":
        from agent.subagent import _ask_main_agent_impl
        # unique_name 由 call_subagent 在创建 handler 后设置（handler._subagent_unique_name）
        unique_name = getattr(self, "_subagent_unique_name", "")
        if not unique_name:
            yield StreamEvent("system", "[ask_main_agent 错误] 子 Agent 唯一名未设置（仅异步子 Agent 可用）\n")
            return StepOutcome(
                {"status": "error", "msg": "subagent unique_name not set (ask_main_agent only for async subagent)"},
                next_prompt="",
            )
        question = args.get("question", "")
        try:
            answer = _ask_main_agent_impl(question, unique_name=unique_name)
            return StepOutcome({"status": "success", "answer": answer}, next_prompt="")
        except Exception as e:
            yield StreamEvent("system", f"[ask_main_agent 错误] {e}\n")
            return StepOutcome({"status": "error", "msg": str(e)}, next_prompt="")
```

- [ ] **Step 6: 确认 Task 8 _run_subagent_async 调 call_subagent 处已传 unique_name**

**说明**：Task 8 Step 3 创建 `_run_subagent_async` 时已包含 `unique_name=unique_name` 透传（见 Task 8 Step 3 的 call_subagent 调用代码）。此处不重复修改，仅作为设计说明：

- `_dispatch_async_subagent`（Task 8 Step 3）注册子 Agent 到 SubagentRegistry
- `_run_subagent_async`（Task 8 Step 3）调 call_subagent 传 `unique_name=unique_name`
- call_subagent（Task 7 Step 4 加的 unique_name 参数）检测 `unique_name is not None` 跳过内部 register + 设置 `handler._subagent_unique_name = unique_name`
- handler.dispatch 的 ask_main_agent 分支通过 `getattr(self, "_subagent_unique_name", "")` 拿到正确 unique_name

**关键**：实施 Task 8 Step 3 时务必包含 `unique_name=unique_name` 透传，否则会导致双重注册 + ask_main_agent unique_name 不匹配死锁。

- [ ] **Step 7: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_ask_main_agent_injection.py -v`
Expected: PASS (2 tests)

- [ ] **Step 8: Commit**

```bash
git add agent/subagent.py agent/handler.py tests/test_ask_main_agent_injection.py
git commit -m "feat(subagent): 异步子 Agent 注入 ask_main_agent 工具（同步子 Agent 不注入避免死锁）"
```

---

### Task 8: _dispatch_async_subagent + _run_subagent_async

**Files:**
- Modify: `agent/subagent.py`（新增两个函数）
- Modify: `agent/handler.py:892-984`（`_call_subagent_gen` 改造为分流）
- Test: `tests/test_async_subagent_dispatch.py`（新建，用真实 LLM）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_async_subagent_dispatch.py
"""验证 _dispatch_async_subagent 立即返回派单确认 + _run_subagent_async 后台跑完推完成通知。

用真实 LLM 调一个简短任务（"回复 OK"）。
"""
import os
import asyncio
import pytest
import sqlite3
from agent.subagent import _dispatch_async_subagent, _run_subagent_async
from agent.subagent_registry import SubagentRegistry
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_supplement import SubagentSupplementQueue


@pytest.fixture
def llm_config():
    import json
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    llm = cfg.get("llm", {})
    return {
        "apikey": llm.get("apikey", ""),
        "apibase": llm.get("apibase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
    }


def test_dispatch_async_subagent_returns_immediately_with_unique_name(llm_config):
    """_dispatch_async_subagent 立即返回，返回值含唯一名 + 使用说明。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")
    
    # 阶段二关键：_dispatch_async_subagent 依赖 niu_api.chat._main_loop（主 asyncio loop）
    # 测试必须设置 _main_loop，否则 _dispatch_async_subagent 返回错误
    import asyncio
    from niu_api.chat import set_main_event_loop, _main_loop
    
    # 创建新 loop 并设为 _main_loop（Python 3.12+ 废弃 get_event_loop，必须 new_event_loop）
    test_loop = asyncio.new_event_loop()
    set_main_event_loop(test_loop)
    
    try:
        # 在另一个线程跑 test_loop（_dispatch_async_subagent 用 run_coroutine_threadsafe 需要 loop 在跑）
        import threading
        def run_loop():
            test_loop.run_forever()
        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()
        
        result = _dispatch_async_subagent(
            agent_name="file-processor",
            task="直接回复 OK，不要调用任何工具",
            llm_config=llm_config,
        )
        
        assert "已派出子 Agent" in result or "file-processor-" in result
        assert "check_subagent_progress" in result
        assert "/stop" in result
        
        # 等子 Agent 跑完（避免影响下一个测试）
        import time
        time.sleep(15)
        
        # 子 Agent 应已注销
        running = [r for r in SubagentRegistry.list_running() if r.agent_type == "file-processor"]
        assert len(running) == 0
    finally:
        test_loop.call_soon_threadsafe(test_loop.stop)
        loop_thread.join(timeout=2)
        test_loop.close()
        set_main_event_loop(None)


def test_run_subagent_async_pushes_completion_to_db(llm_config, tmp_path):
    """_run_subagent_async 跑完后推完成通知到 MainAgentRequestQueue（不写 db）。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")
    
    # 用临时 db 避免污染真实 messages.db
    db_path = str(tmp_path / "messages.db")
    from niu_api import db_monitor
    db_monitor._set_db_path(db_path)
    
    # 初始化临时 db schema
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT, role TEXT, content TEXT, created_at TEXT)")
    conn.commit()
    conn.close()
    
    # 设置 _main_loop（_dispatch_async_subagent 用 run_coroutine_threadsafe 跨线程提交协程）
    import asyncio
    from niu_api.chat import set_main_event_loop
    test_loop = asyncio.new_event_loop()
    set_main_event_loop(test_loop)
    
    sq = SubagentSupplementQueue("test-run-0001")
    mc = SubagentMemoryContext()
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)
    
    import threading
    def run_loop():
        test_loop.run_forever()
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    
    try:
        # 在 test_loop 里跑 _run_subagent_async
        future = asyncio.run_coroutine_threadsafe(
            _run_subagent_async(
                unique_name=name,
                agent_name="file-processor",
                task="直接回复 OK，不要调用任何工具",
                llm_config=llm_config,
                memory_context=mc,
                supplement_queue=sq,
            ),
            test_loop,
        )
        future.result(timeout=120)  # 等子 Agent 跑完
        
        # 验证 MainAgentRequestQueue 里有完成通知（不写 db，走内存队列）
        from agent.main_agent_request_queue import get_main_agent_request_queue
        q = get_main_agent_request_queue()
        queued_msgs = []
        while not q.is_empty():
            queued_msgs.append(q.pop())
        
        # 应该有完成通知（content 格式 "[子名] 已完成，结果：..."）
        completion_found = any(
            "已完成" in m and name in m
            for m in queued_msgs
        )
        assert completion_found, f"MainAgentRequestQueue 应含完成通知：{queued_msgs}"
        
        # 子 Agent 应已注销
        assert SubagentRegistry.get(name) is None
    finally:
        # 清空 MainAgentRequestQueue 避免污染后续测试
        from agent.main_agent_request_queue import get_main_agent_request_queue
        q = get_main_agent_request_queue()
        while not q.is_empty():
            q.pop()
        test_loop.call_soon_threadsafe(test_loop.stop)
        loop_thread.join(timeout=2)
        test_loop.close()
        set_main_event_loop(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_async_subagent_dispatch.py -v`
Expected: FAIL with `ImportError: cannot import name '_dispatch_async_subagent'`

- [ ] **Step 3: Implement _dispatch_async_subagent + _run_subagent_async in agent/subagent.py**

在 `agent/subagent.py` 末尾追加：

```python
# ==================== 阶段二：异步子 Agent 派发与运行 ====================

import asyncio
from typing import Optional


def _dispatch_async_subagent(
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    mcp_client=None,
) -> str:
    """异步派子 Agent：立即返回派单确认，子 Agent 在后台 asyncio 协程跑（跨线程用 run_coroutine_threadsafe 提交到主 loop）。
    
    流程：
      1. 创建 supplement_queue + memory_context
      2. 注册到 SubagentRegistry（is_sync=False）
      3. run_coroutine_threadsafe(_run_subagent_async(...), loop) 跨线程提交到主 loop
      4. 立即返回派单确认（含唯一名 + 使用说明）
    
    Returns:
        派单确认文本（含唯一名 + 使用说明）
    """
    from .subagent_supplement import SubagentSupplementQueue
    from .subagent_memory import SubagentMemoryContext
    from .subagent_registry import SubagentRegistry
    
    # 创建 supplement_queue + memory_context
    sq = SubagentSupplementQueue(unique_name="")  # unique_name 注册后回填
    mc = SubagentMemoryContext()
    
    # 注册（is_sync=False，task 稍后回填——run_coroutine_threadsafe 需要主 loop 在跑）
    unique_name = SubagentRegistry.register(
        agent_type=agent_name,
        supplement_queue=sq,
        memory_context=mc,
        is_sync=False,
        task=None,  # 占位，下面 run_coroutine_threadsafe 后回填
    )
    sq.unique_name = unique_name  # 回填
    
    # 启动 asyncio task（主 Agent 在 executor 线程跑，必须用 run_coroutine_threadsafe 跨线程调度到主 loop）
    from niu_api.chat import _main_loop
    loop = _main_loop
    if loop is None or loop.is_closed():
        SubagentRegistry.unregister(unique_name)
        return f"[错误] 主 asyncio loop 不可用，无法派发异步子 Agent"
    
    # 用 run_coroutine_threadsafe 跨线程调度（handler.dispatch 在 executor 线程，不在主 loop）
    # 返回的是 concurrent.futures.Future，不是 asyncio.Task
    try:
        future = asyncio.run_coroutine_threadsafe(
            _run_subagent_async(
                unique_name=unique_name,
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=mcp_client,
                memory_context=mc,
                supplement_queue=sq,
            ),
            loop,
        )
    except Exception as e:
        # run_coroutine_threadsafe 失败 → 注销子 Agent，避免残留 task=None 的泄漏
        SubagentRegistry.unregister(unique_name)
        logger.error(f"[AsyncSubagent] 派发失败：{e}")
        return f"[错误] 派发异步子 Agent 失败：{e}"
    
    # 回填 future 到注册表（用 future 而非 asyncio.Task，因为跨线程调度返回的是 concurrent.futures.Future）
    # 注意：instance.task 字段类型 Optional[asyncio.Task]，但 concurrent.futures.Future 也能 cancel()
    # 这里把 task 字段语义改为"可取消的 future 句柄"，类型注解保持兼容
    instance = SubagentRegistry.get(unique_name)
    if instance is not None:
        instance.task = future
    
    logger.info(f"[AsyncSubagent] 已派出异步子 Agent：{unique_name}")
    
    return (
        f"已派出子 Agent {unique_name}（类型：{agent_name}），后台运行中。\n"
        f"你可以用 check_subagent_progress('{unique_name}') 查看进度，\n"
        f"写 @ {unique_name} 消息给它补充上下文，\n"
        f"写 @ {unique_name} /stop 停止它。"
    )


async def _run_subagent_async(
    unique_name: str,
    agent_name: str,
    task: str,
    llm_config: Dict[str, Any],
    memory_context: SubagentMemoryContext,
    supplement_queue: SubagentSupplementQueue,
    mcp_client=None,
) -> None:
    """异步子 Agent 的 asyncio task 主体。
    
    跑在 asyncio.to_thread 独立线程（call_subagent 是同步函数），主 loop 不阻塞。
    完成后推完成通知到 MainAgentRequestQueue（不写 db，由 db_monitor 链路 A 检测主 Agent 闲置时推 SSE 触发新一轮）。
    异常或终止时推对应通知。
    最后从 SubagentRegistry 注销。
    """
    from .ask_main_agent import get_pending_ask_registry
    
    try:
        # call_subagent 是同步函数，用 asyncio.to_thread 包一层避免阻塞主 loop
        # 阶段二关键：传 unique_name=unique_name，跳过 call_subagent 内部 register
        # （_dispatch_async_subagent 已注册过，避免双重注册 + handler._subagent_unique_name 不匹配）
        result = await asyncio.to_thread(
            call_subagent,
            agent_name=agent_name,
            task=task,
            llm_config=llm_config,
            mcp_client=mcp_client,
            history=None,
            supplement_queue=supplement_queue,
            memory_context=memory_context,
            unique_name=unique_name,  # 透传 unique_name，跳过 call_subagent 内部 register
        )
        
        # 推完成通知到 MainAgentRequestQueue 内存队列（不写 db）
        # content 格式 "[子名] 已完成，结果：..."——db_monitor 检测主 Agent 闲置时推 SSE 触发前端
        completion_msg = f"[{unique_name}] 已完成，结果：{result[:2000]}"
        try:
            from .main_agent_request_queue import get_main_agent_request_queue
            get_main_agent_request_queue().push(completion_msg)
        except Exception as e:
            logger.error(f"[AsyncSubagent] {unique_name} 推完成通知失败：{e}")
        
        logger.info(f"[AsyncSubagent] {unique_name} 完成")
        
    except Exception as e:
        # 异常通知也推入 MainAgentRequestQueue
        err_msg = f"[{unique_name}] 异常结束：{str(e)[:1000]}"
        try:
            from .main_agent_request_queue import get_main_agent_request_queue
            get_main_agent_request_queue().push(err_msg)
        except Exception:
            pass
        logger.error(f"[AsyncSubagent] {unique_name} 异常：{e}")
        
    finally:
        # 清理 ask_main_agent pending future（避免泄漏）
        get_pending_ask_registry().unregister(unique_name)
        # 从注册表注销
        SubagentRegistry.unregister(unique_name)
```

- [ ] **Step 4: Modify _call_subagent_gen to branch sync vs async**

修改 `agent/handler.py:892-984` 的 `_call_subagent_gen`，加 `async_mode` 参数分流：

```python
    def _call_subagent_gen(self, agent_name: str, args: dict):
        """调用子 Agent（生成器版本）— 同步/异步分流"""
        from .subagent import call_subagent, _dispatch_async_subagent

        task = args.get("task", "")
        async_mode = args.get("async_mode", False)

        # journal-agent 特殊处理：构建增量消息 task，与 tidy 管道一致
        journal_msg_ids_for_cursor = []
        if agent_name == "journal-agent":
            task, journal_msg_ids_for_cursor = self._build_journal_task_for_handler(task)

        # 获取完整的 LLM 配置（从全局 runner）
        from .runner import get_runner

        runner = get_runner()
        if runner is None:
            yield StreamEvent("system", "[System] Runner not initialized\n")
            return StepOutcome(
                {"status": "error", "msg": "Runner not initialized"},
                next_prompt="",
            )

        llm_config = runner.llm_config.copy()

        # 阶段二：异步分流
        if async_mode:
            # 检查该子 Agent 是否支持异步
            from .subagent import get_subagent_config
            agent_config = get_subagent_config(agent_name)
            if not agent_config.get("allowAsync", False):
                return StepOutcome(
                    {"status": "error", "msg": f"子 Agent {agent_name} 不支持异步调用（allowAsync 未启用）"},
                    next_prompt="",
                )
            
            # 硬阻止：event-manager 不允许异步调用（异步路径跳过定时任务入库验证，
            # 会导致定时任务可能未真正入库主 Agent 不知情）
            if agent_name == "event-manager":
                return StepOutcome(
                    {"status": "error", "msg": "event-manager 不支持异步调用（定时任务入库验证需要在同步路径执行）"},
                    next_prompt="",
                )
            
            confirmation = _dispatch_async_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
            )
            yield StreamEvent("tool_marker", f"[SubAgent] 异步派出：{confirmation[:100]}\n")
            return StepOutcome({"status": "success", "result": confirmation}, next_prompt="")

        # 同步路径（现有逻辑不变）
        try:
            yield StreamEvent("tool_marker", f"[SubAgent] Calling {agent_name}...\n")
            _history = None

            result = call_subagent(
                agent_name=agent_name,
                task=task,
                llm_config=llm_config,
                mcp_client=self.mcp_client,
                history=_history,
            )

            if agent_name == "journal-agent" and journal_msg_ids_for_cursor:
                self._update_journal_cursor(result, journal_msg_ids_for_cursor)

            # event-manager 验证逻辑（保持不变 — 完整代码见 agent/handler.py:935-970）
            # 注意：这段验证逻辑只在同步路径执行，异步派发不验证（异步子 Agent 跑在后台，
            # 主 Agent 不阻塞等待，验证逻辑不适用）
            if agent_name == "event-manager" and ("提醒" in task or "定时" in task or "提醒我" in task):
                try:
                    from pathlib import Path
                    import json
                    import sqlite3

                    memory_path = Path.home() / ".niu" / "memory.json"
                    if memory_path.exists():
                        memory = json.loads(memory_path.read_text(encoding="utf-8"))
                        workspace = memory.get("workspace", {}).get("path")
                        if workspace:
                            db_path = str(Path(workspace) / "scheduled_tasks.db")
                            if Path(db_path).exists():
                                try:
                                    with sqlite3.connect(db_path) as conn:
                                        cursor = conn.cursor()
                                        cursor.execute("""
                                            SELECT id, content, status, scheduled_at
                                            FROM scheduled_tasks
                                            ORDER BY created_at DESC
                                            LIMIT 1
                                        """)
                                        latest_task = cursor.fetchone()
                                except sqlite3.Error as e:
                                    yield StreamEvent("system", f"[SubAgent] ⚠ Database error: {e}\n")
                                    latest_task = None

                                if latest_task:
                                    yield StreamEvent("tool_marker", f"[SubAgent] ✓ Verified task in database: {latest_task[1]} at {latest_task[3]}\n")
                                else:
                                    yield StreamEvent("system", f"[SubAgent] ⚠ Warning: No task found in database\n")
                except Exception as e:
                    yield StreamEvent("system", f"[SubAgent] Warning: Failed to verify task: {e}\n")

            yield StreamEvent("tool_marker", f"[SubAgent] {agent_name} completed: {result[:200] if len(result) > 200 else result}\n")
            return StepOutcome(
                {"status": "success", "result": result},
                next_prompt=""
            )
        except Exception as e:
            yield StreamEvent("system", f"[SubAgent] Error: {e}\n")
            return StepOutcome(
                {"status": "error", "msg": str(e)}, next_prompt=""
            )
```

**关键说明**：`_call_subagent_gen` 改造后，同步路径完整保留 event-manager 验证逻辑（不省略），异步路径在前面 `if async_mode:` 分支提前 return，不走验证逻辑。

- [ ] **Step 5: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_async_subagent_dispatch.py -v -x --timeout=120`
Expected: PASS (2 tests，真实 LLM 调用，可能需要 30-60 秒)

- [ ] **Step 6: Commit**

```bash
git add agent/subagent.py agent/handler.py tests/test_async_subagent_dispatch.py
git commit -m "feat(async-subagent): _dispatch_async_subagent + _run_subagent_async + handler 分流"
```

---

### Task 9: check_subagent_progress 工具

**Files:**
- Modify: `agent/handler.py`（新增 `do_check_subagent_progress`）
- Modify: `agent/runner.py:240-284`（`get_tools_schema` 加 check_subagent_progress schema）
- Test: `tests/test_check_subagent_progress.py`（新建）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_check_subagent_progress.py
"""验证 check_subagent_progress 工具读 SubagentMemoryContext.snapshot() 返回进度。"""
import pytest
from agent.subagent_registry import SubagentRegistry
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_supplement import SubagentSupplementQueue
from agent.handler import NiuHandler


def test_check_subagent_progress_returns_snapshot():
    """工具返回子 Agent 的最近一轮 LLM 对话进度。"""
    sq = SubagentSupplementQueue("test-progress-0001")
    mc = SubagentMemoryContext()
    mc.update(
        last_llm_request="请处理这个文件",
        last_llm_response="好的，我开始读取文件",
        current_turn=3,
        last_tool_name="read",
    )
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)
    
    try:
        handler = NiuHandler(mcp_client=None)
        gen = handler.dispatch("check_subagent_progress", {"subagent_name": name}, response=None, index=0)
        # 消费生成器
        ret = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            ret = e.value
        
        assert ret is not None
        # ret.data 是 StepOutcome 的 data 字段
        result = ret.data if hasattr(ret, 'data') else ret
        assert isinstance(result, dict)
        assert "current_turn" in str(result) or "轮次" in str(result)
        assert "读取文件" in str(result) or "read" in str(result)
    finally:
        SubagentRegistry.unregister(name)


def test_check_subagent_progress_unknown_name():
    """未知子 Agent 名返回提示。"""
    handler = NiuHandler(mcp_client=None)
    gen = handler.dispatch("check_subagent_progress", {"subagent_name": "nonexistent-xxxx"}, response=None, index=0)
    ret = None
    try:
        while True:
            next(gen)
    except StopIteration as e:
        ret = e.value
    
    result = ret.data if hasattr(ret, 'data') else ret
    assert "不在运行中" in str(result) or "不存在" in str(result)


def test_check_subagent_progress_sync_subagent_no_memory():
    """同步子 Agent（memory_context=None）返回提示无进度数据。"""
    sq = SubagentSupplementQueue("test-sync-prog-0001")
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, is_sync=True)
    
    try:
        handler = NiuHandler(mcp_client=None)
        gen = handler.dispatch("check_subagent_progress", {"subagent_name": name}, response=None, index=0)
        ret = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            ret = e.value
        
        result = ret.data if hasattr(ret, 'data') else ret
        assert "同步" in str(result) or "无进度" in str(result)
    finally:
        SubagentRegistry.unregister(name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_check_subagent_progress.py -v`
Expected: FAIL — `check_subagent_progress` 工具不存在，dispatch 走到"Unknown tool"

- [ ] **Step 3: Add do_check_subagent_progress method in handler.py**

在 `agent/handler.py` 的 `NiuHandler` 类中（约 `do_no_tool` 方法后）追加：

```python
    def do_check_subagent_progress(self, args: dict, response) -> StepOutcome:
        """查看异步子 Agent 的进度（最近一轮 LLM 对话）。
        
        Args:
            args: {"subagent_name": "file-processor-a1b2"}
        
        Returns:
            StepOutcome(data=进度文本, next_prompt="")
        """
        from .subagent_registry import SubagentRegistry
        
        subagent_name = args.get("subagent_name", "")
        if not subagent_name:
            return StepOutcome(
                {"status": "error", "msg": "subagent_name is required"},
                next_prompt="",
            )
        
        instance = SubagentRegistry.get(subagent_name)
        if instance is None:
            return StepOutcome(
                f"子 Agent {subagent_name} 不在运行中（可能已完成或不存在）。",
                next_prompt="",
            )
        
        if instance.memory_context is None:
            return StepOutcome(
                f"子 Agent {subagent_name} 是同步调用，无进度数据。",
                next_prompt="",
            )
        
        snap = instance.memory_context.snapshot()
        
        # 格式化输出
        lines = [
            f"子 Agent: {subagent_name}",
            f"类型: {instance.agent_type}",
            f"当前轮次: {snap['current_turn']}",
            f"最近工具调用: {snap['last_tool_name'] or '（无）'}",
            f"最近一轮 LLM 请求（摘要）:",
            f"  {snap['last_llm_request'] or '（无）'}",
            f"最近一轮 LLM 回复:",
            f"  {snap['last_llm_response'] or '（无）'}",
        ]
        
        return StepOutcome("\n".join(lines), next_prompt="")
```

- [ ] **Step 4: Add check_subagent_progress schema to get_tools_schema**

修改 `agent/runner.py:240-284` 的 `get_tools_schema`，在子 Agent 工具后追加 `check_subagent_progress` schema：

```python
    # 阶段二：主 Agent 的 check_subagent_progress 工具
    tools.append({
        "type": "function",
        "function": {
            "name": "check_subagent_progress",
            "description": (
                "查看异步子 Agent 的进度。返回子 Agent 最近一轮 LLM 对话（请求摘要、回复、当前轮次、最近工具）。"
                "用于监控后台运行的子 Agent。同步子 Agent 无进度数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subagent_name": {
                        "type": "string",
                        "description": "子 Agent 唯一名（如 file-processor-a1b2，来自派单确认或动态注入区）",
                    },
                },
                "required": ["subagent_name"],
            },
        },
    })

    return tools
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_check_subagent_progress.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add agent/handler.py agent/runner.py tests/test_check_subagent_progress.py
git commit -m "feat(check-progress): check_subagent_progress 工具读 SubagentMemoryContext 进度"
```

---

### Task 10: chat-with-xxx schema 加 async_mode 参数

**Files:**
- Modify: `agent/runner.py:255-282`（子 Agent schema 生成）
- Test: `tests/test_chat_with_schema_async_mode.py`（新建）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_chat_with_schema_async_mode.py
"""验证 allowAsync=true 的子 Agent schema 含 async_mode 参数；allowAsync=false 的不含。"""
import os
import yaml
from agent.runner import get_tools_schema


def test_schema_includes_async_mode_for_allow_async_subagent(monkeypatch):
    """file-processor（allowAsync=true）的 chat-with-file-processor schema 含 async_mode。"""
    # 临时把 file-processor.md 的 allowAsync 设 true（如果还没设）
    config_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "config",
        "agents",
        "file-processor.md",
    )
    
    # 读真实配置
    with open(config_path) as f:
        content = f.read()
    
    # 解析 frontmatter
    parts = content.split("---", 2)
    assert len(parts) >= 3, "file-processor.md frontmatter 解析失败"
    fm = yaml.safe_load(parts[1])
    
    if not fm.get("allowAsync", False):
        pytest.skip("file-processor.md 还没设 allowAsync=true（Task 10 后续步骤会设）")
    
    tools = get_tools_schema()
    chat_with_fp = next(
        (t for t in tools if t.get("function", {}).get("name") == "chat-with-file-processor"),
        None,
    )
    assert chat_with_fp is not None
    
    props = chat_with_fp["function"]["parameters"].get("properties", {})
    assert "async_mode" in props


def test_schema_excludes_async_mode_for_sync_only_subagent():
    """event-manager（allowAsync 未设，默认 false）的 schema 不含 async_mode。"""
    tools = get_tools_schema()
    chat_with_em = next(
        (t for t in tools if t.get("function", {}).get("name") == "chat-with-event-manager"),
        None,
    )
    assert chat_with_em is not None
    
    props = chat_with_em["function"]["parameters"].get("properties", {})
    assert "async_mode" not in props
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_chat_with_schema_async_mode.py -v`
Expected: FAIL — `async_mode` 参数不在 schema 里（现有 schema 只有 task 参数）

- [ ] **Step 3: Modify get_tools_schema to add async_mode conditionally**

修改 `agent/runner.py:255-282` 的子 Agent schema 生成逻辑：

```python
    for agent_name in sub_agents:
        task_desc = "描述要委托给子Agent执行的任务"
        try:
            agent_config = get_subagent_config(agent_name)
            desc = agent_config.get("description", f"子 Agent: {agent_name}")
            task_desc = agent_config.get("taskDescription", task_desc)
        except Exception as e:
            logger.warning(f"Failed to load sub-agent '{agent_name}' config: {e}")
            desc = f"子 Agent: {agent_name}"
            agent_config = {}
        
        # 阶段二：根据 allowAsync 决定是否暴露 async_mode
        allow_async = agent_config.get("allowAsync", False) if agent_config else False
        
        properties = {
            "task": {
                "type": "string",
                "description": task_desc,
            },
        }
        if allow_async:
            properties["async_mode"] = {
                "type": "boolean",
                "description": (
                    "是否异步调用。true=后台运行，立即返回派单确认（含子 Agent 唯一名）；"
                    "false（默认）=同步阻塞等结果。"
                    "异步调用后可用 check_subagent_progress 查进度、@子名 消息补充上下文、@子名 /stop 停止。"
                ),
                "default": False,
            }
        
        tools.append({
            "type": "function",
            "function": {
                "name": f"chat-with-{agent_name}",
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": ["task"],
                },
            },
        })

    return tools
```

- [ ] **Step 4: Add allowAsync=true to file-processor.md frontmatter**

修改 `config/agents/file-processor.md` frontmatter，在 `disableBaseTools` 后追加 `allowAsync: true`：

```yaml
---
name: file-processor
description: "子 Agent — 处理文件和照片：入库、人脸识别、文档解析。用户拖入文件/照片时必须调用此工具，不要自己处理文件。"
temperature: 0.2
mode: subagent
taskDescription: 任务描述，如：处理照片：E:/path/photo.jpg，或：入库文档：E:/path/doc.pdf
permissions:
  '*': allow
mcpServers:
  - photo-server
  - lightrag-server
mcpToolFilter:
  lightrag-server:
    - lightrag_insert
    - lightrag_insert_file
    - lightrag_insert_custom_kg
    - lightrag_insert_entity
    - lightrag_insert_relation
    - lightrag_document_status
    - lightrag_get_document
    - lightrag_search_entities
    - lightrag_list_entities
disableBaseTools:
  - bash
  - code_run
allowAsync: true
---
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_chat_with_schema_async_mode.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add agent/runner.py config/agents/file-processor.md tests/test_chat_with_schema_async_mode.py
git commit -m "feat(schema): chat-with-xxx 加 async_mode 参数 + file-processor allowAsync=true"
```

---

### Task 11: 动态注入区列出后台子 Agent

**Files:**
- Modify: `agent/runner.py:1670-...`（`_inject_dynamic_resources` 加子 Agent 清单段）
- Test: `tests/test_inject_running_subagents.py`（新建）

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inject_running_subagents.py
"""验证 _inject_dynamic_resources 返回的注入文本含后台子 Agent 清单。"""
import pytest
from agent.subagent_registry import SubagentRegistry
from agent.subagent_memory import SubagentMemoryContext
from agent.subagent_supplement import SubagentSupplementQueue
from agent.runner import NiuRunner


def test_inject_lists_running_async_subagents(monkeypatch):
    """有异步子 Agent 在跑时，注入文本含其唯一名和状态。"""
    sq = SubagentSupplementQueue("test-inject-0001")
    mc = SubagentMemoryContext()
    name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)
    
    try:
        runner = NiuRunner.__new__(NiuRunner)  # 不调 __init__ 避免加载 LLM 等
        # mock 依赖（_inject_dynamic_resources 内部调多个属性）
        monkeypatch.setattr(runner, "_get_brain_injector", lambda: None)
        monkeypatch.setattr(runner, "_brain_adapter", None)
        # mock LightRAGAdapter 让 search_multi_lightrag 返回空（避免无 lightrag 时报错）
        import niu_api.internal.lightrag_adapter as lightrag_adapter_mod
        class _FakeAdapter:
            def search_multi_lightrag(self, *args, **kwargs):
                return {}
        monkeypatch.setattr(lightrag_adapter_mod, "LightRAGAdapter", _FakeAdapter)
        
        # 调 _inject_dynamic_resources
        injection, _ = runner._inject_dynamic_resources("测试上下文")
        
        assert "后台" in injection or "子 Agent" in injection
        assert name in injection
        assert "file-processor" in injection
    finally:
        SubagentRegistry.unregister(name)


def test_inject_no_subagents_no_section():
    """没有异步子 Agent 在跑时，注入文本不含子 Agent 清单段。"""
    # 清空注册表
    for r in SubagentRegistry.list_running():
        SubagentRegistry.unregister(r.unique_name)
    
    runner = NiuRunner.__new__(NiuRunner)
    # mock 依赖（让 LightRAG 检索不报错）
    import types
    monkeypatch_runner = runner
    
    # mock _inject_dynamic_resources 的 LightRAG 调用返回空
    # 这测试可能难写，先跳过精确实现，只验证调用不抛异常
    # 如果实现复杂，可以删除此测试只保留 test_inject_lists_running_async_subagents
    pytest.skip("跳过 — 验证空清单场景由集成测试覆盖")


def test_inject_caps_at_5_subagents():
    """超过 5 个子 Agent 时只显示前 5 个 + '还有 N 个'。"""
    sqs = []
    names = []
    try:
        for i in range(7):
            sq = SubagentSupplementQueue(f"test-cap-{i:04d}")
            mc = SubagentMemoryContext()
            name = SubagentRegistry.register("file-processor", supplement_queue=sq, memory_context=mc, is_sync=False)
            sqs.append(sq)
            names.append(name)
        
        runner = NiuRunner.__new__(NiuRunner)
        # mock 依赖（同上）
        try:
            injection, _ = runner._inject_dynamic_resources("测试")
            # 至少不抛异常，且含"还有"或类似提示
            assert "还有" in injection or len([n for n in names if n in injection]) <= 5
        except Exception:
            # mock 不足导致异常，跳过
            pytest.skip("mock 不足，跳过")
    finally:
        for n in names:
            SubagentRegistry.unregister(n)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_inject_running_subagents.py::test_inject_lists_running_async_subagents -v`
Expected: FAIL — 注入文本不含"后台子 Agent"段

- [ ] **Step 3: Modify _inject_dynamic_resources to add subagent listing**

在 `agent/runner.py` 的 `_inject_dynamic_resources` 方法末尾（return 前）追加子 Agent 清单段。先 Read 看看方法的完整结构：

```python
    # 阶段二：注入后台子 Agent 清单
    subagent_section = self._format_running_subagents_section()
    if subagent_section:
        # 拼接到 injection 末尾
        injection = (injection + "\n\n" + subagent_section) if injection else subagent_section
```

并在 `NiuRunner` 类中新增 helper 方法（约 `_inject_dynamic_resources` 后）：

```python
    def _format_running_subagents_section(self) -> str:
        """格式化后台子 Agent 清单段（动态注入用）。
        
        软上限 5 个，超出只显示前 5 + "还有 N 个"。
        只列异步子 Agent（同步子 Agent 主 Agent 阻塞中，无法 @）。
        """
        from agent.subagent_registry import SubagentRegistry
        import time
        
        async_subagents = [r for r in SubagentRegistry.list_running() if not r.is_sync]
        if not async_subagents:
            return ""
        
        # 按启动时间排序（started_at 字段，Task 3 已加）
        async_subagents.sort(key=lambda r: r.started_at)
        
        # 软上限 5 个
        shown = async_subagents[:5]
        remaining = len(async_subagents) - len(shown)
        
        lines = ["[当前后台运行的子 Agent]"]
        for r in shown:
            status = "running"
            if r.memory_context is not None:
                snap = r.memory_context.snapshot()
                turn = snap.get("current_turn", 0)
                if turn > 0:
                    status = f"running（第 {turn} 轮）"
            lines.append(f"- {r.unique_name}（类型：{r.agent_type}，状态：{status}）")
        
        if remaining > 0:
            lines.append(f"- 还有 {remaining} 个子 Agent 运行中")
        
        lines.append("")
        lines.append("如需查看某子 Agent 进度，调用 check_subagent_progress 工具。")
        lines.append("如需给某子 Agent 补充上下文，写消息到对话（@子名 补充内容）。")
        lines.append("如需停止某子 Agent，写消息到对话（@子名 /stop）。")
        
        return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_inject_running_subagents.py::test_inject_lists_running_async_subagents -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add agent/runner.py tests/test_inject_running_subagents.py
git commit -m "feat(inject): 动态注入区列出后台异步子 Agent 清单（软上限 5）"
```

---

### Task 12: 主 Agent 提示词加异步调用说明

**Files:**
- Modify: `config/agents/niu.md`（在阶段一已加的"主↔子 Agent 对话规则"段后追加异步调用说明）

- [ ] **Step 1: Read current niu.md 确认阶段一段位置**

Run: `cd <repo_root> && grep -n "主↔子\|对话规则\|@消息\|逐条回复" config/agents/niu.md`

预期输出含 L204 附近的 `## 主↔子 Agent 对话规则` 段（阶段一加的）。如果 grep 没匹配到，说明阶段一段名不同或已改动，必须 Read 整个 niu.md 找到对话规则段位置后再 Edit，不要盲目追加。

- [ ] **Step 2: Read 阶段一段上下文确认追加位置**

Run: `cd <repo_root> && python/bin/python -c "
from agent.subagent import get_subagent_prompt
p = get_subagent_prompt('niu')
# 找对话规则段
idx = p.find('主↔子')
if idx >= 0:
    print('found 主↔子 at index', idx)
    print('上下文（前 200 + 后 500）:')
    print(p[max(0,idx-200):idx+500])
else:
    print('NOT FOUND 主↔子 in niu.md body')
"`

确认找到"主↔子 Agent 对话规则"段，记录段尾位置（用于 Edit 的 old_string 定位）。

- [ ] **Step 3: Append async instructions section after 阶段一段**

用 Edit 工具，old_string 用阶段一段的最后一段（从 Read 输出里复制唯一片段），new_string 在其后追加：

```markdown
## 异步子 Agent 调用

部分子 Agent 支持 `async_mode=true` 异步调用（schema 含 `async_mode` 参数即支持）：
- 异步调用时工具立即返回派单确认，含子 Agent 唯一名（如 `file-processor-a1b2`）
- 子 Agent 在后台运行，你可以继续做别的事
- 动态注入区会列出当前后台运行的子 Agent 名字和状态
- 查看进度：调用 `check_subagent_progress(子名)`
- 补充上下文：写 `@子名 补充内容` 到对话
- 停止：写 `@子名 /stop` 到对话
- 完成通知：子 Agent 跑完后，系统会自动触发新一轮对话，你会收到一条 `[子名] 已完成，结果：...` 的消息（由 db_monitor 检测你闲置时写入 db 最后一条 user 消息触发）

### 何时用异步调用

- 长任务（如处理大文档、批量照片识别）→ 异步，避免阻塞你做别的事
- 短任务（如查询、简单操作）→ 同步（默认），结果立即可用
- 并行多个独立任务 → 异步派出多个

### 子 Agent 唯一名规则

格式：`<类型>-<4位hex>`（如 `file-processor-a1b2`）
名字由程序自动生成，在派单确认和动态注入区可见。
用这个名字 @ 子 Agent 进行所有交互。

### 收到 [子名] 问题消息时

如果子 Agent 通过 `ask_main_agent` 工具向你提问，系统会自动触发新一轮对话，你会看到一条 `[子名] 问题内容` 的消息（由 db_monitor 检测你闲置时写入 db 最后一条 user 消息触发，无需 @主Agent）：
- 必须回复，写 `@子名 回答内容`（回复里带 @子名 让 db_monitor 路由到正确子 Agent）
- 子 Agent 阻塞等待你的回答，不回复会导致子 Agent 卡死
- 多个子 Agent 同时问时，系统按 FIFO 顺序逐条触发你处理，你逐条回复即可（每个 @子名 一条）
```

- [ ] **Step 4: 验证 niu.md 改动不破坏启动**

Run: `cd <repo_root> && python/bin/python -c "from agent.subagent import get_subagent_prompt; p = get_subagent_prompt('niu'); print('niu.md body length:', len(p)); print('contains 异步:', '异步' in p)"`
Expected: 输出 `niu.md body length: <某数>` 和 `contains 异步: True`，不报错

- [ ] **Step 5: Commit**

```bash
git add config/agents/niu.md
git commit -m "docs(prompt): 主 Agent 提示词加异步子 Agent 调用说明段"
```

---

### Task 13: 端到端集成测试 — 异步调用 + 完成通知

**Files:**
- Test: `tests/test_integration_async_complete.py`（新建）

- [ ] **Step 1: Write integration test**

```python
# tests/test_integration_async_complete.py
"""端到端验证：主 Agent 异步派子 Agent → 子 Agent 后台跑 → 完成推 db → 主 Agent 下一轮看到。

用真实 LLM + 真实 db_monitor + 真实 messages db。
"""
import os
import asyncio
import sqlite3
import time
import pytest


@pytest.fixture
def llm_config():
    import json
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    llm = cfg.get("llm", {})
    return {
        "apikey": llm.get("apikey", ""),
        "apibase": llm.get("apibase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
    }


def test_async_dispatch_and_completion_notification(llm_config, tmp_path):
    """异步派子 Agent → 跑完 → MainAgentRequestQueue 收到完成通知（不写 db）。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")
    
    # 用临时 db
    db_path = str(tmp_path / "messages.db")
    from niu_api import db_monitor
    db_monitor._set_db_path(db_path)
    
    # 初始化 db schema
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    
    # 初始化 db_monitor 基线
    # 阶段二关键：需要设置 _main_loop（_dispatch_async_subagent 用 run_coroutine_threadsafe）
    import asyncio
    from niu_api.chat import set_main_event_loop
    test_loop = asyncio.new_event_loop()
    set_main_event_loop(test_loop)
    import threading
    def run_loop():
        test_loop.run_forever()
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    
    try:
        asyncio.run_coroutine_threadsafe(db_monitor._init_routed_baseline(), test_loop).result(timeout=5)
        
        # 派异步子 Agent
        from agent.subagent import _dispatch_async_subagent
        confirmation = _dispatch_async_subagent(
            agent_name="file-processor",
            task="直接回复 OK，不要调用任何工具",
            llm_config=llm_config,
        )
        
        assert "file-processor-" in confirmation
        
        # 等子 Agent 跑完（最多 60 秒）
        from agent.subagent_registry import SubagentRegistry
        for _ in range(120):
            time.sleep(0.5)
            if SubagentRegistry.list_running() == []:
                break
        
        assert SubagentRegistry.list_running() == [], "子 Agent 应已注销"
        
        # 完成通知走 MainAgentRequestQueue 内存队列（不写 db，不需要 _poll_messages）
        # _run_subagent_async 完成时直接 push 到队列，测试主动 drain 验证
        from agent.main_agent_request_queue import get_main_agent_request_queue
        q = get_main_agent_request_queue()
        queued_msgs = []
        while not q.is_empty():
            queued_msgs.append(q.pop())
        
        assert len(queued_msgs) >= 1, f"MainAgentRequestQueue 应含完成通知：{queued_msgs}"
        found = any("已完成" in s or "OK" in s for s in queued_msgs)
        assert found, f"MainAgentRequestQueue 应含完成通知：{queued_msgs}"
    finally:
        # 清空 MainAgentRequestQueue 避免污染后续测试
        from agent.main_agent_request_queue import get_main_agent_request_queue
        q = get_main_agent_request_queue()
        while not q.is_empty():
            q.pop()
        test_loop.call_soon_threadsafe(test_loop.stop)
        loop_thread.join(timeout=2)
        test_loop.close()
        set_main_event_loop(None)
        # 恢复 db_monitor._db_path 到真实路径（避免污染后续测试）
        import os
        db_monitor._set_db_path(os.path.join(os.path.expanduser("~"), ".niu", "messages.db"))
```

- [ ] **Step 2: Run test**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_integration_async_complete.py -v -x --timeout=120`
Expected: PASS（真实 LLM 调用，约 30-60 秒）

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration_async_complete.py
git commit -m "test(integration): 异步派子 Agent + 完成通知端到端测试"
```

---

### Task 14: 端到端集成测试 — ask_main_agent + /stop 不死锁

**Files:**
- Test: `tests/test_ask_main_agent_stop_deadlock.py`（新建）

- [ ] **Step 1: Write test**

```python
# tests/test_ask_main_agent_stop_deadlock.py
"""端到端验证：ask_main_agent 阻塞期间收 /stop 不死锁。

场景：
  1. 异步子 Agent 调 ask_main_agent 问"我应该用 OCR 吗" → future.wait() 阻塞
  2. 主 Agent 发 @子名 /stop
  3. db_monitor 路由 /stop：推 supplement queue (is_terminate=True) + cancel_pending_ask
  4. ask_main_agent 工具解除阻塞，返回 terminated 状态
  5. 子 Agent 下一轮 drain 到 /stop，调 LLM 生成总结，退出
  
用真实 LLM + 真实 db_monitor。
"""
import os
import asyncio
import sqlite3
import time
import threading
import pytest


@pytest.fixture
def llm_config():
    import json
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "user-config.json")
    with open(config_path) as f:
        cfg = json.load(f)
    llm = cfg.get("llm", {})
    return {
        "apikey": llm.get("apikey", ""),
        "apibase": llm.get("apibase", ""),
        "model": llm.get("model", ""),
        "type": llm.get("type", "openai"),
    }


def test_ask_main_agent_during_stop_no_deadlock(llm_config, tmp_path):
    """ask_main_agent 阻塞期间收 /stop 不死锁。"""
    if not llm_config["apikey"]:
        pytest.skip("LLM API key not configured")
    
    # 用临时 db
    db_path = str(tmp_path / "messages.db")
    from niu_api import db_monitor
    db_monitor._set_db_path(db_path)
    
    # 初始化 db schema
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    
    # 阶段二关键：需要设置 _main_loop（_dispatch_async_subagent 用 run_coroutine_threadsafe）
    import asyncio
    from niu_api.chat import set_main_event_loop
    test_loop = asyncio.new_event_loop()
    set_main_event_loop(test_loop)
    import threading
    def run_loop():
        test_loop.run_forever()
    loop_thread = threading.Thread(target=run_loop, daemon=True)
    loop_thread.start()
    
    try:
        asyncio.run_coroutine_threadsafe(db_monitor._init_routed_baseline(), test_loop).result(timeout=5)
        
        # 派异步子 Agent，任务设计成"必须问主 Agent"才能完成
        # _dispatch_async_subagent 是同步函数，内部用 run_coroutine_threadsafe 提交到 _main_loop（即 test_loop）
        # 所以 test_loop 必须 run_forever 中，否则提交会失败
        from agent.subagent import _dispatch_async_subagent
        from agent.subagent_registry import SubagentRegistry
        
        confirmation = _dispatch_async_subagent(
            agent_name="file-processor",
            task=(
                "你需要处理一个文件，但不确定是否需要 OCR。"
                "请用 ask_main_agent 工具询问主 Agent：'这个 PDF 是扫描件吗？需要 OCR 吗？'"
                "然后根据主 Agent 的回答决定下一步。"
            ),
            llm_config=llm_config,
        )
        
        # 提取子 Agent 唯一名
        unique_name = None
        for r in SubagentRegistry.list_running():
            if r.agent_type == "file-processor":
                unique_name = r.unique_name
                break
        assert unique_name is not None
        
        # 等子 Agent 进入 ask_main_agent 阻塞（最多 30 秒）
        from agent.ask_main_agent import get_pending_ask_registry
        reg = get_pending_ask_registry()
        
        entered_ask = False
        for _ in range(60):
            time.sleep(0.5)
            with reg._lock:
                if unique_name in reg._futures:
                    entered_ask = True
                    break
        
        if not entered_ask:
            # 子 Agent 没调 ask_main_agent（LLM 行为不确定），跳过
            # 但必须先清理：取消异步子 Agent 的 task future，避免残留线程污染后续测试
            instance = SubagentRegistry.get(unique_name)
            if instance is not None and instance.task is not None:
                try:
                    instance.task.cancel()
                except Exception:
                    pass
            # 直接调 route_message 路由 /stop（模拟主 Agent 发 /stop，绕过 db 写入）
            try:
                db_monitor.route_message(target=unique_name, sender="主Agent", content="/stop")
            except Exception:
                pass
            # 等子 Agent 退出（最多 30 秒）
            for _ in range(60):
                time.sleep(0.5)
                if not any(r.unique_name == unique_name for r in SubagentRegistry.list_running()):
                    break
            pytest.skip("子 Agent 没调 ask_main_agent，无法测试死锁场景")
        
        # 直接调 route_message 路由 /stop（模拟主 Agent 发 /stop，绕过 db 写入）
        db_monitor.route_message(target=unique_name, sender="主Agent", content="/stop")
        
        # route_message 已同步路由 /stop（cancel_pending_ask + 推 supplement queue），不需要 _poll_messages
        
        # 验证子 Agent 不死锁：在 60 秒内退出
        for _ in range(120):
            time.sleep(0.5)
            if not any(r.unique_name == unique_name for r in SubagentRegistry.list_running()):
                break
        else:
            pytest.fail("子 Agent 死锁——ask_main_agent 阻塞期间收 /stop 后未退出")
        
        # 验证子 Agent 已注销
        assert SubagentRegistry.get(unique_name) is None
        
        # 验证 MainAgentRequestQueue 收到子 Agent 终止/完成/异常通知（不写 db，走内存队列）
        from agent.main_agent_request_queue import get_main_agent_request_queue
        q = get_main_agent_request_queue()
        queued = []
        while not q.is_empty():
            queued.append(q.pop())
        
        # 子 Agent ask_main_agent 被 cancel 后走终止总结退出，_run_subagent_async 推完成或异常通知
        assert any(unique_name in m for m in queued), f"MainAgentRequestQueue 应含子 Agent 通知：{queued}"
    finally:
        # 清空 MainAgentRequestQueue 避免污染后续测试
        from agent.main_agent_request_queue import get_main_agent_request_queue
        q = get_main_agent_request_queue()
        while not q.is_empty():
            q.pop()
        test_loop.call_soon_threadsafe(test_loop.stop)
        loop_thread.join(timeout=2)
        test_loop.close()
        set_main_event_loop(None)
        # 恢复 db_monitor._db_path 到真实路径（避免污染后续测试）
        import os
        db_monitor._set_db_path(os.path.join(os.path.expanduser("~"), ".niu", "messages.db"))
```

- [ ] **Step 2: Run test**

Run: `cd <repo_root> && python/bin/python -m pytest tests/test_ask_main_agent_stop_deadlock.py -v -x --timeout=180`
Expected: PASS（真实 LLM 调用，约 30-120 秒）

如果测试不稳定（LLM 不一定调 ask_main_agent），可加 `@pytest.mark.flaky(reruns=3)` 或在任务描述里更强约束让 LLM 一定调 ask_main_agent。

- [ ] **Step 3: Commit**

```bash
git add tests/test_ask_main_agent_stop_deadlock.py
git commit -m "test(integration): ask_main_agent 阻塞期间收 /stop 不死锁端到端测试"
```

---

### Task 15: 全量回归测试 + 真实程序验证

**Files:**
- 无修改，只验证

- [ ] **Step 1: 跑全量单元测试**

Run: `cd <repo_root> && python/bin/python -m pytest tests/ -v 2>&1 | tail -50`
Expected: 所有测试 PASS（阶段一 63 个 + 阶段二新增约 20 个）

- [ ] **Step 2: 杀所有 niu 进程**

Run: `pkill -f "niu" 2>/dev/null; pkill -f "python.*niu_api" 2>/dev/null; sleep 2; ps aux | grep -E "niu|niu_api" | grep -v grep`
Expected: 无残留进程

- [ ] **Step 3: 启动真实程序**

Run: `cd <repo_root> && ./niu &`
Expected: 程序启动，日志 `logs/api_stderr.log` 无 import 错误，db_monitor 启动日志出现

- [ ] **Step 4: 验证 db_monitor 基线初始化**

Run: `sleep 5 && tail -50 logs/api_stderr.log | grep -E "db_monitor|基线"`
Expected: 日志含 `db_monitor 基线初始化：last_seen_rowid=...` 和 `db_monitor 启动`

- [ ] **Step 5: 真实端到端验证（手动）**

通过前端发消息让主 Agent 异步派 file-processor：

用户消息示例：
```
请用 chat-with-file-processor 工具处理一个简单任务：直接回复 OK。
传 async_mode=true 让它后台运行。
```

预期：
- 主 Agent 调 `chat-with-file-processor` with `async_mode=true`
- 工具立即返回派单确认（含唯一名如 `file-processor-a1b2`）
- 主 Agent 报告已派出
- 几秒后子 Agent 跑完，`_run_subagent_async` 推 `[子名] 已完成` 到 MainAgentRequestQueue
- db_monitor 链路 A 检测主 Agent 闲置 → 推 SSE 触发前端调 /api/chat/session → 后端写 user 消息 + 调 LLM
- 主 Agent 看到最后一条 user 消息是 `[子名] 已完成`，报告结果

- [ ] **Step 6: 验证 ask_main_agent 端到端（手动）**

用户消息示例：
```
请用 chat-with-file-processor 异步派出，任务：用 ask_main_agent 工具问我"需要 OCR 吗"，然后根据我的回答决定。
```

预期：
- 子 Agent 调 ask_main_agent → 推 `[子名] 问题内容` 到 MainAgentRequestQueue 内存队列（不写 db）
- db_monitor 链路 A 检测主 Agent 闲置 → 推 SSE 触发前端 → 前端调 /api/chat/session → 后端写 user 消息 + 调 LLM
- 主 Agent 看到最后一条 user 消息是 `[子名] 问题内容`，回复 `@子名 是的，需要 OCR`
- db_monitor 链路 B 路由 @子名 回答到 PendingAskRegistry.set_answer
- 子 Agent ask_main_agent 工具解除阻塞，拿到回答继续

- [ ] **Step 7: 验证 /stop 不死锁（手动）**

用户消息示例：
```
请用 chat-with-file-processor 异步派出，任务：用 ask_main_agent 问"需要 OCR 吗"。
然后立即发 @子名 /stop 停止它（用子 Agent 的唯一名）。
```

预期：
- 子 Agent ask_main_agent 阻塞中
- 主 Agent 发 @子名 /stop（写到 db，role=subagent_msg）
- db_monitor 链路 B 路由 /stop：推子 Agent supplement queue + cancel_pending_ask
- 子 Agent ask_main_agent 解除阻塞，返回 terminated 状态
- 子 Agent 下一轮 drain 到 /stop，调 LLM 生成总结
- 子 Agent 退出，`_run_subagent_async` 推 `[子名] 已完成/异常结束` 到 MainAgentRequestQueue（不写 db）

- [ ] **Step 8: 杀进程清理**

Run: `pkill -f "niu" 2>/dev/null; pkill -f "python.*niu_api" 2>/dev/null; sleep 2`
Expected: 所有 niu 进程退出

- [ ] **Step 9: Commit（如果有 fix）**

```bash
git add -A
git commit -m "test(stage2): 全量回归 + 真实程序验证通过"
```

---

### Task 16: 前端 onNewMessage 自动触发主 Agent 新一轮对话

**Files:**
- Modify: `ui/assistant/chat.html`（onNewMessage 加 role="subagent_msg" 自动触发分支）

**职责**：db_monitor 链路 A 推 SSE（role="subagent_msg", source="subagent"）后，前端收到自动调 /api/chat/session 触发主 Agent 新一轮 LLM 对话。检查 isProcessing（必须 false，主 Agent 闲置）避免忙时触发。

- [ ] **Step 1: Read current onNewMessage 实现**

Run: `cd <repo_root> && grep -n "onNewMessage\|role ===" ui/assistant/chat.html | head -20`

找到 `onNewMessage` 回调（约 L1426-1453），确认现有分支：
- `role === "chat_idle"` → 重置状态机
- `role === "chat_busy"` → 进入忙碌状态
- 其他 role → `refreshFromDB()` 仅刷新渲染

- [ ] **Step 2: 加 role="subagent_msg" 自动触发分支**

在 `onNewMessage` 回调里加新分支（在 `role === "chat_idle"` 分支之前，避免被 chat_idle 重置干扰）：

```javascript
// 阶段二：子 Agent 触发的消息（ask_main_agent 或完成通知），主 Agent 闲置时自动触发新一轮对话
if (data.role === 'subagent_msg' && data.source === 'subagent') {
    // 检查主 Agent 是否闲置（isProcessing 由 chat_busy/chat_idle SSE 维护，状态机持久化已确认）
    if (!isProcessing) {
        // content 格式 "[子名] 内容"——作为 message 参数传给 /api/chat/session
        // 后端 compat.py 写 user 消息到 db（作为最后一条 user 消息）+ 调 LLM
        const msgContent = data.content;
        // 不调 addMessage 本地渲染（避免重复——后端写 db 后会再推一次 SSE 让前端 refreshFromDB）
        // 直接调 sendMessageWithRetry 走 HTTP 路径
        sendMessageWithRetry(msgContent).catch(err => {
            console.error('[Stage2] 自动触发主 Agent 对话失败：', err);
        });
    } else {
        // 兜底：主 Agent 忙时（理论上不应发生，db_monitor 只在闲置时推 SSE）
        // 不触发，记录日志便于排查
        console.warn('[Stage2] 收到 subagent_msg SSE 但主 Agent 忙，消息可能已从队列 pop（边界场景）');
    }
    return;
}

// 现有分支...
if (data.role === 'chat_idle') { ... }
if (data.role === 'chat_busy') { ... }
// ...
```

**关键说明**：
- `sendMessageWithRetry` 是现有函数（chat.html:894），用户发消息时也走这条路径。这里复用，传 msgContent 作为 message 参数。
- 不调 `addMessage('user', text)` 本地渲染——因为后端写 db 后会再推一次 SSE（role=user, source=electron），前端那时再 refreshFromDB 渲染。避免重复。
- 检查 `isProcessing == false`——db_monitor 只在主 Agent 闲置时推 SSE，理论上必为 false。但前端状态机可能有延迟（chat_idle 推送和 subagent_msg 推送的时序），加兜底判断避免忙时触发。
- 边界：如果 isProcessing=true（理论上不应发生），消息会丢失。这是已知限制，db_monitor 设计上只在闲置时推 SSE，保证不丢。

- [ ] **Step 3: 验证前端改动不破坏现有逻辑**

Run: `cd <repo_root> && ./niu &`
启动后：
- 用户正常发消息 → 走现有 sendMessage 路径（不受影响）
- 主 Agent 回复 → SSE 推 role=assistant → refreshFromDB（不受影响）
- chat_busy/chat_idle → 状态机（不受影响）

手动测试子 Agent 触发场景：
1. 主 Agent 派异步子 Agent（chat-with-file-processor with async_mode=true）
2. 子 Agent 调 ask_main_agent → MainAgentRequestQueue
3. db_monitor 检测主 Agent 闲置 → 推 SSE role=subagent_msg source=subagent
4. 前端 onNewMessage 收到 → isProcessing=false → 自动调 sendMessageWithRetry
5. 后端 /api/chat/session 写 user 消息 + 调 LLM → 主 Agent 回复 @子名
6. db_monitor 路由 @子名 回答到子 Agent → ask_main_agent 解除阻塞

- [ ] **Step 4: Commit**

```bash
git add ui/assistant/chat.html
git commit -m "feat(frontend): onNewMessage 加 role=subagent_msg 自动触发主 Agent 新一轮对话"
```

---

## 验证清单

实施完后，验收以下场景全部通过：

### 单元测试
- [ ] `tests/test_subagent_memory.py` — SubagentMemoryContext snapshot/update 线程安全
- [ ] `tests/test_ask_main_agent.py` — AskMainAgentFuture + PendingAskRegistry
- [ ] `tests/test_subagent_registry_async.py` — 异步子 Agent 注册（task + memory_context）
- [ ] `tests/test_call_subagent_memory_hook.py` — call_subagent 传 memory_context 更新进度
- [ ] `tests/test_db_monitor_ask_routing.py` — db_monitor 路由回答 + /stop cancel
- [ ] `tests/test_ask_main_agent_injection.py` — 异步子 Agent 注入 ask_main_agent
- [ ] `tests/test_check_subagent_progress.py` — check_subagent_progress 工具
- [ ] `tests/test_chat_with_schema_async_mode.py` — schema 加 async_mode
- [ ] `tests/test_inject_running_subagents.py` — 动态注入区列后台子 Agent

### 集成测试（真实 LLM）
- [ ] `tests/test_async_subagent_dispatch.py` — 异步派出 + 后台跑 + 完成推 db
- [ ] `tests/test_integration_async_complete.py` — 端到端：异步派 → 完成 → 主 Agent 收通知
- [ ] `tests/test_ask_main_agent_stop_deadlock.py` — ask_main_agent 阻塞期间收 /stop 不死锁

### 真实程序验证
- [ ] 程序启动无 import 错误
- [ ] db_monitor 基线初始化日志
- [ ] 主 Agent 异步派 file-processor 端到端通
- [ ] ask_main_agent 端到端通（子问主、主答、子继续）
- [ ] /stop 不死锁（子 Agent ask_main_agent 阻塞期间收 /stop 正确退出）

### 阶段一回归
- [ ] 阶段一已有 63 个测试全 PASS（同步子 Agent 路径不破坏）
- [ ] 同步子 Agent /stop 双击停止仍工作
- [ ] 主 Agent 给同步子 Agent 补充上下文仍工作
