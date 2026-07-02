# 主 Agent ↔ 子 Agent 交互通道设计

**日期**：2026-07-02
**状态**：设计待审查
**相关记忆**：`main-subagent-no-interaction-channel`、`stop-button-bug-rootcause`、`subagent-deadloop-three-defenses`

## 目标

解决主 Agent 与子 Agent 之间的架构缺陷：当前是单向同步阻塞调用，主 Agent 调子 Agent 后完全阻塞，无法通信、无法看进度、无法优雅停止。

本次升级实现：
1. **双向对话**——子 Agent 能问主 Agent、主 Agent 能给子 Agent 补充上下文
2. **进度可见**——主 Agent 能查看子 Agent 最近一轮 LLM 对话
3. **异步调用**——主 Agent 可派子 Agent 后台运行，自己继续做别的事
4. **优雅终止**——停止信号灯重新设计，/stop 指令协作式终止，不误杀后台子 Agent

## 整体架构

两条独立通道，职责不重叠：

### 通道一：messages db + `@` 标识 + db 监测程序（通信通道）

承担所有"主↔子"消息类通信：子问主、主补充子、停止指令、完成通知。复用现有 messages db，加 `@` 标识约定和一个 db 监测程序路由。

### 通道二：动态注入 + 进度查看工具（进度通道）

承担"主看子进度"。子 Agent 上下文是内存、一次性的不写 db，所以进度单独走一条通道：后台子 Agent 注册表 + 动态注入区列出名字 + `check_subagent_progress` 工具读内存上下文。

### 两条通道的职责划分

| | 通道一 | 通道二 |
|---|---|---|
| 介质 | messages db（持久） | 内存上下文（一次性） |
| 解决 | 通信、指令、完成通知 | 进度可见 |
| 子 Agent 写 db？ | 写（问主、完成通知） | 不写 |
| 主 Agent 读 db？ | 读（看到 @ 消息） | 不读（用工具查内存） |

异步调用横跨两条通道：派单走通道一（MCP 工具立即返回派单确认含子 Agent 唯一名），子 Agent 跑在 asyncio task 注册到通道二的注册表，完成通知走通道一（子写 db，主下一轮处理）。

---

## 通道一详细设计

### `@` 消息格式约定

所有主↔子通信消息走 messages db，统一格式：

```
@<目标> [<发送者名>] <内容>
```

- **目标**：`主Agent` 或 `<子Agent唯一名>`（如 `file-processor-a1b2`）
- **发送者名**：发送方唯一名（子 Agent 发送时带自己名字；主 Agent 发送时可省略）
- **内容**：普通文本 / `/stop` 指令 / 完成通知

示例：
```
@主Agent [file-processor-a1b2] 这个 PDF 是扫描件，需要 OCR 吗？
@file-processor-a1b2 是的，请用 OCR 处理
@file-processor-a1b2 /stop
@主Agent [file-processor-a1b2] 已完成，结果：识别出 3 个人脸...
```

**db 存储方式**：复用现有 messages 表，加 `role="subagent_msg"`。现有 messages 表 schema 无 metadata 列，目标/发送者名直接编码进 content 前缀（即 `@目标 [发送者名] 内容` 格式本身就是存储格式，靠解析 content 提取）。零 schema 改动，无需迁移。前端读 `role="subagent_msg"` 的消息按特殊样式渲染（带 `@` 标识区分主↔子对话与用户↔主对话）。

**前端渲染样式**：`chat.html` 的 `addMessage` 加 `role === "subagent_msg"` 分支：
- 解析 content `@目标 [发送者名] 实际内容`，拆成"发送者→目标"的标签 + 实际内容
- 渲染为侧边小卡片样式（小字号、浅灰背景、左边带"子→主"或"主→子"方向箭头图标），区别于用户消息和主 Agent 回复
- 不带头像（避免与主 Agent 回复混淆）
- 卡片内显示"发送者 → 目标"标签 + 实际内容

**history 重建时的过滤**：`agent_loop.py:309` 的 history 合并逻辑按 `role` 分支处理，`role="subagent_msg"` 的历史消息**必须过滤掉**，不能塞进 LLM 上下文——否则会被当 user 输入污染主 Agent 的 LLM 请求。具体：在 history 重建时跳过 `role == "subagent_msg"` 的消息（这些消息的语义是"主↔子对话"，主 Agent 通过 supplement queue 消费，不进 history）。子 Agent 的 history 由 `call_subagent` 自己构造（不读主 Agent 的 messages db），所以子 Agent 也不会看到 `subagent_msg`。

### db 监测程序（后台 asyncio task）

常驻 asyncio task，职责：**轮询 messages db 中未处理的 `@` 消息，按目标路由**。

```
每 200ms 轮询：
  查 db 中 role="subagent_msg" 且未标记"已路由"的新消息
  解析 @目标
  if 目标 == "主Agent":
    → 暂存"待推送主 Agent"队列（等主 Agent 空闲时推入 supplement queue）
  elif 目标 == 某个在跑子 Agent 名:
    解析内容
    if 内容 == "/stop":
      → 推入该子 Agent supplement queue，标记 is_terminate=True
    else:  # 普通补充
      → 推入该子 Agent supplement queue，标记 is_terminate=False
  else:  # 目标不在跑
    → 记日志"消息目标未知，丢弃或推回主 Agent"
```

**监测程序是唯一的 db 路由中枢**——主和子都不直接读对方的 `@` 消息，都通过监测程序路由。

**db 监测程序生命周期**：
- **启动**：在 niu_api 启动时（uvicorn lifespan startup）`asyncio.create_task` 启动监测程序
- **退出**：应用 shutdown 时 `task.cancel()`，监测程序捕获 `CancelledError` 优雅退出
- **崩溃重启**：监测程序自身 `try/except Exception` 捕获异常，崩溃后 `await asyncio.sleep(1)` 后重启循环（外层 `while True:` 包裹）
- **心跳日志**：每 60 秒记一条"监测程序存活，路由了 N 条消息"日志，便于发现停滞

### 主 Agent supplement queue 与"逐条回复"约束

主 Agent supplement queue 里可能有多个 `@主Agent` 消息（多个子 Agent 同时问）。

主 Agent system prompt 静态约束：
> 当 supplement queue 里有 `@主Agent` 消息时，必须**逐条回复**，不能只回最后一个。每条回复都要带发送者名字（`@子名 回答`），让 db 监测程序能路由到正确子 Agent。

主 Agent 下一轮 LLM 调用前：
- supplement queue 里的 `@主Agent` 消息作为次末信息插入（复用现有用户补充消息的机制）
- 主 Agent 回复后，回复推回 db（`@子名 回答`），db 监测程序路由到对应子 Agent
- 子 Agent 的会话工具拿到回答，工具返回，子 Agent 继续

### 主 Agent 空闲时才推送的约束（修正：复用现有 enqueue_supplement）

**原设计假设**：监测程序检查主 Agent 状态，空闲时才推入 supplement queue。

**审查发现**：现有代码无"主 Agent 空闲"钩子，`drain_supplement()` 只在 `agent_loop.py:701` 一处被调用（下一轮 LLM 调用前）。监测程序无法可靠判断主 Agent 是否空闲。

**修正设计**：监测程序**不判断空闲**，直接调 `enqueue_supplement`（现有用户补充消息的机制），让 `drain_supplement` 在主 Agent 下一轮 LLM 调用前自然消费。

**代价**：主 Agent 调一个长工具时，子 Agent 的 `@主Agent` 消息会延迟到该工具调用结束、下一轮 LLM 调用前才被看到。这是合理代价——与现有用户给主 Agent 发补充消息的行为一致，主 Agent 调工具时用户的补充也是等下一轮才看到。

**好处**：零新增机制，复用现有 supplement queue 全套逻辑（enqueue / drain / 次末插入）。

### 子 Agent 的"会话工具"（子问主）

子 Agent 用的 MCP 工具 `ask_main_agent`：

```
工具名：ask_main_agent
参数：question (str)
行为：
  1. 生成消息 "@主Agent [<自己唯一名>] <question>"
  2. 推到 messages db
  3. 阻塞等待（工具不返回），直到 db 监测程序路由来主 Agent 的回答
  4. 拿到回答后工具返回，子 Agent 继续下一轮
```

**阻塞机制**：子 Agent 调这个工具时，工具内部用 `threading.Event` + 共享 dict 存 answer 实现阻塞。子 Agent 跑在 `asyncio.to_thread` 独立线程，`Event.wait()` 阻塞安全；主 loop 的 db 监测程序路由到回答时 `event.set()` + 写 answer 到共享 dict，跨线程安全。具体：

```python
class AskMainAgentFuture:
    def __init__(self):
        self._event = threading.Event()
        self._answer: str | None = None

    def set_answer(self, answer: str):
        self._answer = answer
        self._event.set()

    def wait(self, timeout=None) -> str | None:
        self._event.wait(timeout=timeout)
        return self._answer
```

子 Agent 调 `ask_main_agent` 时创建 `AskMainAgentFuture`，注册到"待回答问题表"（**key=子 Agent 唯一名**，value=Future），推 db 后 `future.wait()` 阻塞。db 监测程序解析主 Agent 回答消息的 `@目标` 拿子名 → 查"待回答问题表"找到对应 Future → `future.set_answer(answer)`。子 Agent 拿到 answer 后工具返回。

**key 用子 Agent 唯一名而不是问题消息 rowid**：因为 `ask_main_agent` 工具阻塞子 Agent 循环，同一子 Agent 同时只有一个 Future 在等，按子名路由唯一且简单。回答消息是新 rowid，无法从回答反推原问题 rowid，所以 key 不能用 rowid。

**所有异步调用的子 Agent 默认带这个工具**（在 `build_subagent_system_segments` 自动注入 schema）。同步调用的子 Agent 不带（避免死锁，见 §异步调用-同步兼容）。

### 子 Agent 独立 supplement queue（关键新建）

**现状**：`agent/subagent.py:241` 硬编码 `enable_supplement=False`（注释"False for sub-agents to prevent stealing main agent's supplements"）。原因是现有 supplement queue 是全局单例（`agent/runner.py:44 _supplement_queue`），主子共享会串话。所以子 Agent 当前不消费任何 supplement。

**新建设计**：每个子 Agent 实例持有一个独立 `queue.Queue`（线程安全，同步/异步子 Agent 都用同一类型）。

```python
# agent/subagent_supplement.py（新建）

import queue as _queue
import threading

@dataclass
class SubagentSupplementItem:
    content: str
    is_terminate: bool  # /stop 标记
    sender: str         # 发送者名（如 "主Agent"）

class SubagentSupplementQueue:
    """每个子 Agent 实例一个，线程安全。"""
    def __init__(self, unique_name: str):
        self.unique_name = unique_name
        self._q = _queue.Queue()

    def push(self, content: str, is_terminate: bool = False, sender: str = "主Agent"):
        self._q.put_nowait(SubagentSupplementItem(content, is_terminate, sender))

    def drain(self) -> list[SubagentSupplementItem]:
        items = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except _queue.Empty:
                break
        return items
```

**子 Agent 消费时机**：`_run_agent_loop` 改造 `enable_supplement=True`，`drain_supplement` 改为读自己的 `SubagentSupplementQueue`。**关键改造点**：`agent_runner_loop`（`agent_loop.py:273-290`）现有 `drain_supplement()` 在 `agent_loop.py:701` 是直接 `from agent.runner import drain_supplement` 硬编码全局函数。改造方式：给 `agent_runner_loop` 加可选参数 `supplement_drain: Callable[[], list] | None = None`，None 时走现有全局 drain（主 Agent 路径不变），非 None 时调子 Agent 自己的 drain（`SubagentSupplementQueue.drain()`）。`_run_agent_loop` 调 `agent_runner_loop` 时把 `supplement_queue.drain` 作为 `supplement_drain` 传入。每轮 LLM 调用前 drain：
- 普通补充（`is_terminate=False`）→ 次末位插入
- /stop 终止（`is_terminate=True`）→ 最末位插入"总结后终止"

**db 监测程序路由**：监测程序在主 loop，路由 `@子名` 消息时，按 unique_name 从 SubagentRegistry 拿到该子 Agent 的 `SubagentSupplementQueue`，调 `push()`（`queue.Queue.put_nowait` 线程安全，跨线程无障碍）。

**与主 Agent supplement queue 的隔离**：主 Agent 仍用现有全局 `_supplement_queue`（`runner.py:44`），子 Agent 用自己的独立 queue。两者不串话。

### 同步子 Agent 的 supplement_queue 来源（阶段一关键）

**问题**：阶段一同步子 Agent 也要响应主补充子 + 双击停止批量 /stop，但同步子 Agent 不注册到 SubagentRegistry（同步调用不需要唯一名）。db 监测程序路由 `@子名` 时从注册表拿 queue——同步子 Agent 不在注册表，路由失败。

**解决方案**：同步子 Agent 也有一个临时 `SubagentSupplementQueue`，由 `call_subagent` 创建并参数注入 `_run_agent_loop`（不进注册表）。

**同步子 Agent 的命名**：同步子 Agent 也需要一个唯一名（db 监测程序路由 `@子名` 要用）。`call_subagent` 启动时生成 `<agent_type>-<4位hex>` 唯一名，存入一个"同步子 Agent 注册表"（独立于 SubagentRegistry，或合并到 SubagentRegistry 但标记 `is_sync=True`）。

**同步子 Agent 注册表的生命周期**：
- `call_subagent` 启动时：生成唯一名 + 创建 supplement_queue + 注册到"同步子 Agent 注册表"
- `call_subagent` 结束时（无论正常/异常）：从注册表移除
- 注册表项含：unique_name、supplement_queue、thread_id（可选，便于调试）

**双击停止的统一路由**：双击停止按钮触发批量 /stop 时，db 监测程序遍历：
- SubagentRegistry 中所有异步子 Agent 的 supplement_queue
- 同步子 Agent 注册表中所有同步子 Agent 的 supplement_queue
- 给每个 queue 推 `is_terminate=True` 的 /stop 项

**主 Agent 写 `@同步子名 /stop` 的路由**：db 监测程序先查 SubagentRegistry（异步），再查同步子 Agent 注册表（同步），找到对应 queue 推 /stop。

**合并注册表设计（推荐）**：为避免两个注册表，可把同步子 Agent 也注册到 SubagentRegistry，加字段 `is_sync: bool` + `task: None`（同步子 Agent 没有 asyncio task）。`list_running()` 返回所有（含同步异步），双击停止统一遍历。同步子 Agent 的 `memory_context` 可选（阶段一不需要进度查看，同步子 Agent 的 memory_context 可设 None）。

**阶段一同步子 Agent 不暴露 `check_subagent_progress`**：同步子 Agent 在主 Agent 阻塞中，主 Agent 无法调工具查进度。所以同步子 Agent 不需要 memory_context，注册表项的 `memory_context` 字段对同步子 Agent 设 None。

### /stop 指令的处理流程

```
主 Agent 写 @子名 /stop 到 db
  ↓
db 监测程序识别 "/stop" 关键字
  ↓
推入该子 Agent supplement queue，标记 is_terminate=True
  ↓
子 Agent 当前工具调用跑完，下一轮 LLM 调用前检查 supplement queue
  ↓
看到 is_terminate=True，最末位插入"收到终止指令，请总结本轮工作后终止，不要再调用工具"
  ↓
子 Agent 调 LLM，大模型输出总结（不调工具）
  ↓
子 Agent agent_runner_loop 检测"无工具调用"，自然退出循环
  ↓
子 Agent 推完成通知到 db：@主Agent [子名] 已终止，总结：...
  ↓
子 Agent 从后台注册表移除，asyncio task 结束
```

**普通补充 vs /stop 的插入位置**（关键区别）：
- 普通补充消息 → **次末位**（倒数第二），最后一条是子 Agent 当前任务指令，子 Agent 继续正常工作
- /stop 终止指令 → **最末位**（最后一条），内容是"总结后终止"，大模型直接输出总结不调工具

两者都进 supplement queue，子 Agent 下一轮 LLM 调用前消费。

**协作式等待**：/stop 推入 supplement queue 后，子 Agent 当前正在执行的工具调用让它跑完（避免工具中途取消留下脏状态），下一轮 LLM 调用前才消费 /stop。

### 终止信号灯重新设计

**现状**：`_stop_requested` 全局 threading.Event，主子共享，子清空会误杀主（记忆 `stop-button-bug-rootcause` 的 Bug 2）。

**新设计**：
- **`_stop_requested` 信号灯只对主 Agent 有效**——子 Agent 运行时不监测它
- **双击停止按钮** → 触发给所有在跑子 Agent 发 `/stop`（db 监测程序批量路由）
- **单击停止按钮** → 只设 `_stop_requested`，主 Agent 自己检查退出，子 Agent 不受影响（继续跑）
- **主 Agent 主动停某个子 Agent** → 主写 `@子名 /stop` 到 db，db 监测程序路由

**单击停止的 UX 提示**：单击后如果 SubagentRegistry 还有在跑的子 Agent，前端弹提示"已停主 Agent，N 个子 Agent 仍在运行，双击全部停止"。避免用户以为停止按钮失灵。

**双击窗口与 Escape 协调**：
- **双击窗口**：400ms 内第二次单击 = 双击，触发批量 /stop
- **Escape 键**：仍走单击路径（只停主 Agent），与现有 Escape 绑定一致
- **实现**：单击后启动 400ms 定时器，定时器内第二次单击 = 双击（取消定时器，触发批量 /stop）；定时器超时 = 单击生效（只停主）

消费端逻辑完全一样——都是 /stop 指令走最末插入。

---

## 通道二详细设计

### 后台子 Agent 注册表

全局注册表（内存数据结构），维护当前在跑的异步子 Agent：

```python
# agent/subagent_registry.py（新建）

import threading

@dataclass
class RunningSubagent:
    unique_name: str          # file-processor-a1b2
    agent_type: str           # file-processor
    task: asyncio.Task        # 子 Agent 的 asyncio task
    memory_context: SubagentMemoryContext
    supplement_queue: SubagentSupplementQueue  # 子 Agent 独立 supplement queue
    started_at: float
    status: str               # "running" | "asking_main" | "terminated"

class SubagentRegistry:
    _instances: dict[str, RunningSubagent] = {}
    _lock = threading.Lock()  # 保护 register/unregister/list_running 的 read-modify-write

    @classmethod
    def register(cls, agent_type: str, task, memory_context, supplement_queue) -> str:
        with cls._lock:
            unique_name = cls._gen_unique_name(agent_type)  # 检查碰撞重试
            cls._instances[unique_name] = RunningSubagent(...)
            return unique_name

    @classmethod
    def unregister(cls, unique_name): ...
    @classmethod
    def list_running(cls) -> list[RunningSubagent]:
        with cls._lock:
            return list(cls._instances.values())
    @classmethod
    def get(cls, unique_name) -> RunningSubagent | None: ...
```

**唯一名生成**：`<agent_type>-<4位hex>`（如 `file-processor-a1b2`）。4 位 hex 有 65536 种组合，同一 agent_type 同时跑 65536 个不重名，实际够用。生成时检查注册表避免碰撞（在 `_lock` 内做 read-modify-write）。

**加锁原因**：异步子 Agent 跑在 `asyncio.to_thread` 独立线程，注册/注销发生在主 loop（派单时）和子线程（结束时 `finally`）。Python dict 单个操作 GIL 下原子，但 register 生成唯一名时"检查碰撞再写入"是 read-modify-write 非原子，用 `threading.Lock` 保护。

### 子 Agent 内存上下文（进度数据来源）

子 Agent 跑的时候，`_run_agent_loop` 维护一个"最近一轮 LLM 对话"的内存对象：

```python
@dataclass
class SubagentMemoryContext:
    last_llm_request: str | None    # 最近一轮送给 LLM 的内容（system+history+user 拼接摘要，或完整 prompt）
    last_llm_response: str | None   # LLM 最近一轮的回复文本（不含工具调用）
    current_turn: int               # 当前第几轮
    last_tool_name: str | None      # 最近一次调的工具名（可选辅助信息）
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        """一次性拷贝所有字段，保证主 Agent 读到一致状态。"""
        with self._lock:
            return {
                "last_llm_request": self.last_llm_request,
                "last_llm_response": self.last_llm_response,
                "current_turn": self.current_turn,
                "last_tool_name": self.last_tool_name,
            }

    def update(self, **kwargs):
        """子 Agent 线程更新字段，加锁保证一致性。"""
        with self._lock:
            for k, v in kwargs.items():
                setattr(self, k, v)
```

**更新时机**：子 Agent 的 `agent_runner_loop` 每轮调完 LLM 后，调 `memory_context.update(last_llm_request=..., last_llm_response=..., ...)`。内存对象不进 db，子 Agent 结束后随注册表移除而消失。

**snapshot 方法**：主 Agent 调 `check_subagent_progress` 时，用 `snapshot()` 一次性拷贝，避免读到 `current_turn=5` 但 `last_llm_response` 还是 turn 4 的不一致状态。

**只存最近一轮**：主 Agent 想看更早的进度，那是上一轮查过的（主 Agent 自己记住），或等子 Agent 完成通知后看总结。

### 动态注入：列出后台子 Agent

主 Agent 系统提示词的动态注入区，每轮注入"当前后台运行的子 Agent 清单"：

```
[当前后台运行的子 Agent]
- file-processor-a1b2（启动于 3 分钟前，状态：running）
- context-manager-c3d4（启动于 1 分钟前，状态：asking_main）

如需查看某子 Agent 进度，调用 check_subagent_progress 工具。
如需给某子 Agent 补充上下文，写消息到对话（@子名 补充内容）。
如需停止某子 Agent，写消息到对话（@子名 /stop）。
```

**注入时机**：复用现有 `_inject_dynamic_resources`（每轮结束刷新）或 `_on_turn_end` 回调，从 `SubagentRegistry.list_running()` 拉清单拼成文本注入。

**数量上限**：软上限 5 个。超出时只显示最近 5 个 + "还有 N 个子 Agent 运行中"。避免 10 个子 Agent × 每个 3 行 = 30 行挤占主 Agent 上下文。

**状态显示**：
- `running`：正常跑
- `asking_main`：子 Agent 调了 `ask_main_agent` 在等主 Agent 回答（主 Agent 看到此状态就知道有 `@主Agent` 消息要回复）
- `terminated`：正在终止流程（已收到 /stop，等当前工具调用完）

### 进度查看工具

主 Agent 的新工具 `check_subagent_progress`：

```
工具名：check_subagent_progress
参数：subagent_name (str)  # 子 Agent 唯一名
行为：
  1. 从 SubagentRegistry.get(subagent_name) 拿 RunningSubagent
  2. 如果子 Agent 不存在或已结束，返回"该子 Agent 不在运行中"
  3. 如果 memory_context is None（同步子 Agent），返回"该子 Agent 是同步调用，无进度数据"
  4. 调 memory_context.snapshot() 一次性拷贝（加锁保证一致性）
  5. 返回格式化文本：
     子 Agent: file-processor-a1b2
     当前轮次: 5
     最近一轮 LLM 请求（摘要）:
       <last_llm_request 的前 N 字符或摘要>
     最近一轮 LLM 回复:
       <last_llm_response>
     最近工具调用: read_file
```

**这个工具暴露给主 Agent**：在主 Agent 的 MCP 工具列表里加（不是子 Agent 的工具）。

**工具调用是同步的**：主 Agent 调 `check_subagent_progress` 时，工具直接读内存返回，不阻塞。主 Agent 拿到进度信息后继续自己的工作。

**同步子 Agent 不暴露给此工具**：同步子 Agent 的 `memory_context=None`，且同步子 Agent 调用时主 Agent 阻塞无法调工具。动态注入区只列异步子 Agent（同步子 Agent 的唯一名仅供前端展示和注册表内部用，不暴露给主 Agent 写 `@` 消息或调 `check_subagent_progress`）。

### 两条通道的边界

通道二**只负责进度可见**，不承担通信：
- 子 Agent 不写 db 进度（避免污染主 Agent 上下文库）
- 主 Agent 不直接读子 Agent 内存（必须通过 `check_subagent_progress` 工具）
- 子 Agent 结束后，memory_context 随注册表移除而消失（一次性）

通信（问主、补充、停止、完成通知）全走通道一（db）。

---

## 异步调用与 MCP 工具改造

### 子 Agent 配置：`allowAsync` 标识

子 Agent 的 `.md` frontmatter 加标识：

```markdown
---
name: file-processor
description: 子 Agent — 文件处理专用
mcpServers: [...]
disableBaseTools: [bash, grep]
allowAsync: true   # ← 新增，默认 false
---
```

**默认 false**：现有 6 个子 Agent（context-manager / entity-extractor / dream-evolver / event-manager / file-processor / journal-agent）默认 `allowAsync: false`，保持现有同步行为不变。

**设为 true 的子 Agent**：长任务、可并行、无冲突风险的（如 file-processor 处理大文档）。具体哪些子 Agent 设 true 由用户决定，本次只做机制不做配置调整。

### MCP 工具 schema 改造

`chat-with-xxx` 工具的 schema 加可选参数 `async_mode`：

```json
{
  "name": "chat-with-file-processor",
  "description": "子 Agent — 文件处理专用。...",
  "parameters": {
    "type": "object",
    "properties": {
      "task": {"type": "string", "description": "任务描述"},
      "async_mode": {
        "type": "boolean",
        "description": "是否异步调用。true=后台运行，立即返回派单确认；false（默认）=同步阻塞等结果。仅 allowAsync=true 的子 Agent 支持 async_mode=true。",
        "default": false
      }
    },
    "required": ["task"]
  }
}
```

**schema 动态生成**：`build_subagent_tool_schema` 根据 `allowAsync` 标识决定是否暴露 `async_mode` 参数。`allowAsync: false` 的子 Agent schema 不含 `async_mode`，主 Agent 无法选异步。

### handler.dispatch 的 `chat-with-*` 分支改造

```
现有：tool_name.startswith("chat-with-") → _call_subagent_gen（同步阻塞）

改造后：
  解析 args 拿 async_mode
  if not async_mode:
    → 走现有同步路径 _call_subagent_gen（不变）
  else:
    → 检查该子 Agent allowAsync
      if false: → 返回错误"该子 Agent 不支持异步调用"
      if true: → _dispatch_async_subagent（新函数）
```

### `_dispatch_async_subagent` 函数

```
def _dispatch_async_subagent(agent_name, task, ...):
    1. 生成唯一名：agent_type + 4位hex（如 file-processor-a1b2）
    2. 创建 SubagentMemoryContext 空对象
    3. 创建 asyncio task 跑子 Agent：
         task = asyncio.create_task(_run_subagent_async(unique_name, agent_name, task, memory_context))
    4. 注册到 SubagentRegistry
    5. 立即返回派单确认：
       "已派出子 Agent {unique_name}（类型：{agent_name}），后台运行中。
        你可以用 check_subagent_progress('{unique_name}') 查看进度，
        写 @ {unique_name} 消息给它补充上下文，
        写 @ {unique_name} /stop 停止它。"
    → 主 Agent 工具循环拿到这个返回值，退出循环继续做别的
```

**关键点**：
- asyncio task 启动后立即返回，主 Agent 不阻塞
- 派单确认含唯一名 + 使用说明
- 子 Agent 在 asyncio task 里跑，共享主 asyncio loop

### `_run_subagent_async` 函数（asyncio task 主体）

```
async def _run_subagent_async(unique_name, agent_name, task, memory_context):
    try:
        result = await asyncio.to_thread(call_subagent, agent_name, task, ..., memory_context=memory_context)
        _push_to_db(f"@主Agent [{unique_name}] 已完成，结果：{result}")
    except TerminateSignal:
        _push_to_db(f"@主Agent [{unique_name}] 已终止，总结：{result}")
    except Exception as e:
        _push_to_db(f"@主Agent [{unique_name}] 异常结束：{str(e)}")
    finally:
        SubagentRegistry.unregister(unique_name)
```

**运行时要点**：

- **call_subagent 改造**：现有 `call_subagent` 是同步函数，签名扩展加 `memory_context: SubagentMemoryContext | None = None` 可选参数。用 `asyncio.to_thread` 包一层，子 Agent 跑独立线程，避免 GIL 阻塞主 asyncio loop 的 LLM 调用。跨线程通信（supplement queue / ask_main_agent 阻塞）用线程安全原语（`queue.Queue`、`asyncio.run_coroutine_threadsafe` 注入主 loop）。

- **memory_context 更新钩子**：在 `agent_runner_loop`（`agent_loop.py:273` 起）内部，`client.chat(messages=messages, ...)` 调用前（`agent_loop.py:413`）snapshot `messages`（拼一个摘要或取最后一条 user content）写入 `memory_context.last_llm_request`；`client.chat` 返回后把响应文本写入 `memory_context.last_llm_response`；检测 `StreamEvent.type == "tool_marker"` 时记录 `last_tool_name`；每轮结束 `current_turn += 1`。

  **注意**：钩子位置在 `agent_runner_loop`（不是 `_run_agent_loop`），因为 `client.chat` 在 `agent_runner_loop` 内部。`_run_agent_loop` 只是驱动 `agent_runner_loop` 生成器，不直接调 `client.chat`。`agent_runner_loop` 需加可选参数 `memory_context: SubagentMemoryContext | None = None`，None 时跳过所有更新（主 Agent 路径不变）。

  现有 `StreamEvent` 类型只有 `system`/`reply`/`persist`/`tool_marker` 四种（`agent_loop.py:10`），**没有 `llm_request` 类型**——LLM 请求内容在 `messages` 列表里不通过 StreamEvent 暴露。所以 `last_llm_request` 必须在 `client.chat` 调用前直接从 `messages` 提取，不能靠检测 chunk 类型。`last_llm_response` 可从 `reply` 类型的 chunk 拿（或直接从 `client.chat` 返回值拿）。

  `memory_context is None` 时不影响现有同步调用路径（所有更新加 `if memory_context is not None:` 守卫）。

- **子 Agent 不监测 `_stop_requested`**：现有 `_run_agent_loop`（`subagent.py:251`）的 `is_stop_requested()` 检查移除，改为只检查自己的 `SubagentSupplementQueue`（drain 时看是否有 `is_terminate=True` 项）。现有同步调用停止行为会改变：原同步调用时主 Agent 按停止，子 Agent 也停；新设计是子 Agent 不停（只响应自己的 /stop）。这是预期行为（与"信号灯只对主 Agent 有效"一致）。

- **子 Agent 的 supplement queue**：每个子 Agent 有自己的 supplement queue（`queue.Queue` 或 `asyncio.Queue`）。db 监测程序路由来的 `@子名` 消息推入。子 Agent 下一轮 LLM 调用前消费。

### 同步调用路径的兼容

同步调用（`async_mode=false` 或不传）走现有路径，**不变**：
- 仍调 `_call_subagent_gen` → `call_subagent` 同步阻塞
- 主 Agent 等子 Agent 跑完
- 子 Agent 不注册到 SubagentRegistry（不是后台跑）
- 子 Agent 没有唯一名（同步调用不需要）
- 子 Agent 不响应 /stop（同步调用中主 Agent 自己停就行）

**同步子 Agent 不带 `ask_main_agent`**：同步调用时主 Agent 阻塞等子 Agent，子 Agent 如果调 `ask_main_agent` 问主 Agent，主 Agent 阻塞在 `call_subagent` 不会回答 → 死锁。所以 `ask_main_agent` 只在异步调用时注入给子 Agent。同步子 Agent 遇到歧义用现有"直接退出"机制（记忆 `subagent-deadloop-three-defenses`）。

### 主 Agent 提示词改造

主 Agent system prompt（静态部分）加异步调用说明：

```
[异步子 Agent 调用]
部分子 Agent 支持 async_mode=true 异步调用（schema 有 async_mode 参数即支持）。
异步调用时：
- 工具立即返回派单确认，含子 Agent 唯一名（如 file-processor-a1b2）
- 子 Agent 在后台运行，你可以继续做别的事
- 动态注入区会列出当前后台运行的子 Agent 名字和状态
- 查看进度：调 check_subagent_progress(名字)
- 补充上下文：写 @名字 补充内容（作为消息发到对话）
- 停止：写 @名字 /stop
- 收到 @主Agent [名字] 消息时：逐条回复，每条带发送者名字

[子 Agent 唯一名规则]
格式：<类型>-<4位hex>（如 file-processor-a1b2）
名字由程序自动生成，在派单确认和动态注入区可见。
用这个名字 @ 子 Agent 进行所有交互。
```

### 异步子 Agent 的并发管理

- **不限数量**：主 Agent 可同时派多个异步子 Agent
- **同名子 Agent**：唯一名后缀保证不重名，主 Agent 能区分
- **资源保护**：如果担心子 Agent 太多拖垮系统，后续可加上限（如最多 5 个）。本次先不做，按需加。

---

## 错误处理与边界情况

### 子 Agent OOM/崩溃

子 Agent 跑在 `asyncio.to_thread` 独立线程，崩溃会抛异常被 `_run_subagent_async` 的 `except Exception` 捕获，推 `@主Agent [子名] 异常结束：...` 到 db，从注册表移除。主 Agent 下一轮看到异常通知处理。

**无进程级隔离**：子 Agent OOM 仍可能拖垮主进程（同进程架构限制）。本次不做进程隔离，后续如出现故障可考虑子解释器或独立进程。

### db 监测程序崩溃

db 监测程序是常驻 asyncio task，崩溃后所有 `@` 消息停止路由。需要加：
- 监测程序自身异常捕获，崩溃后自动重启
- 监测程序心跳日志，便于发现停滞

### asyncio task 取消

主 Agent 整体被停止（用户单击停止按钮）时，主 Agent 退出但异步子 Agent 不受影响（继续跑）。如果用户想停所有子 Agent，双击停止按钮触发批量 /stop。

### 子 Agent 超时无响应

本次不实现超时机制。子 Agent 卡死时，主 Agent 可以通过 `check_subagent_progress` 发现长时间没新轮次，主动发 /stop。如果 /stop 也无响应（子 Agent 卡在某个工具调用里不回 loop），只能等用户双击停止或重启程序。后续可加子 Agent 心跳 + 强制取消机制。

### 子 Agent 问主 Agent 但主 Agent 已退出

主 Agent 退出后，db 监测程序仍运行（如果是独立 task）。子 Agent 的 `ask_main_agent` 阻塞等待，主 Agent 不再回复 → 子 Agent 永远阻塞。

**解决**：`ask_main_agent` 加超时（如 5 分钟），超时后返回"主 Agent 未响应，请自行决定或退出"。本次先不做超时，标记为已知限制。

### db 残留 `@子名` 消息在程序重启后

唯一名是内存的（注册表），程序重启后 db 里残留 `@file-processor-a1b2` 消息，监测程序找不到目标子 Agent。

**处理**：监测程序发现 `@目标` 不在 SubagentRegistry 时，把消息转为 `@主Agent [system] 历史消息目标子 Agent {name} 已不存在：{内容}` 推入主 Agent supplement queue，让主 Agent 知道有残留消息，由主 Agent 决定是否处理。

### 孤儿回答（子 Agent 问主 Agent 后崩溃）

子 Agent 调 `ask_main_agent` 后崩溃，主 Agent 回复推回 db，监测程序路由时找不到子 Agent 的 Future。

**处理**：监测程序检测到目标子 Agent 已不在注册表时，把回答转为 `@主Agent [system] 孤儿回答：{内容}` 推入主 Agent supplement queue，让主 Agent 决定是否丢弃。

### 双重广播风险（已知限制第 5 条）澄清

原描述"chat_queue 调 `_tidy_context_impl` 可能与子 Agent 完成通知产生序列问题"实际场景是：`_on_context_high_usage`（runner.py:807）跑 force 压缩时调 `call_subagent("context-manager")`，此时主 Agent 在压缩回调中。如果异步子 Agent 推完成通知到 db，监测程序路由到主 supplement queue，主 Agent 还在压缩循环里——`drain_supplement` 不会被调用直到压缩结束。这不是"双重广播"，是"压缩期间 supplement 排队延迟"，与"主 Agent 空闲时才推送"修正后的行为一致（主 Agent 下一轮自然消费）。不阻塞，是预期行为。

---

## 测试策略

### 单元测试

- **db 监测程序路由**：mock messages db，推入不同 `@目标` 的消息，验证路由到正确 supplement queue
- **唯一名生成**：验证 `agent_type + 4位hex` 格式、碰撞重试
- **/stop 识别**：验证 `/stop` 关键字解析、is_terminate 标记
- **supplement 插入位置**：普通补充进次末、/stop 进最末
- **ask_main_agent 阻塞与返回**：mock db 监测程序，验证工具阻塞等待、拿到回答后返回

### 集成测试

- **阶段一端到端**：同步调用子 Agent，子 Agent 调 `ask_main_agent` 问主 Agent，主 Agent 回答，子 Agent 继续——验证双向通信
- **阶段二端到端**：异步调用子 Agent，主 Agent 派出后继续做别的，子 Agent 完成推 db 通知，主 Agent 下一轮处理——验证异步 + 完成通知
- **进度查看**：异步子 Agent 跑几轮，主 Agent 调 `check_subagent_progress`，验证返回最近一轮 LLM 对话
- **/stop 终止**：异步子 Agent 跑起来，主 Agent 发 /stop，验证子 Agent 总结后退出、推完成通知
- **双击停止**：多个异步子 Agent 在跑，双击停止按钮，验证所有子 Agent 收到 /stop

### 测试约束

按项目铁律（记忆 `real-testing-only`）：测试必须用真实程序 + 真实 LLM，禁止 mock 测试。但 db 监测程序的路由逻辑可以 mock db 测试（不涉及 LLM）。集成测试必须真实起子 Agent 跑。

---

## 实施分阶段

### 阶段一：通信通道（在现有同步调用基础上）

范围：
- **子 Agent 独立 supplement queue**（新建 `agent/subagent_supplement.py`，`_run_agent_loop` 改造 `enable_supplement=True` 并 drain 自己的 queue）
- db 监测程序（后台 asyncio task，轮询 + 路由）
- `@` 消息格式约定 + db 存储字段（`role="subagent_msg"`，目标/发送者名编码进 content 前缀，零 schema 改动）
- 主 Agent 给子 Agent 补充上下文（次末插入，走子 Agent supplement queue）
- /stop 终止指令（最末插入 + 协作式等待，走子 Agent supplement queue）
- 终止信号灯重新设计（`_stop_requested` 只对主 Agent，子 Agent 移除 `is_stop_requested()` 检查，双击触发批量 /stop）
- 主 Agent 提示词约束（逐条回复 + 命名规则——命名规则在阶段二用，但提示词可先写）
- 前端 chat.html 渲染 `role="subagent_msg"` 消息（特殊样式区分 `@` 消息）
- 双击停止按钮 UI（单击只停主，双击触发批量 /stop）

**阶段一不做 `ask_main_agent`**：`ask_main_agent` 需要子 Agent 异步跑（主 Agent 不阻塞才能回答）。阶段一同步调用子 Agent 时主 Agent 阻塞在 `call_subagent`，子 Agent 调 `ask_main_agent` 会死锁。所以 `ask_main_agent` 留到阶段二异步调用时注入。

**阶段一同步子 Agent /stop 的局限**：同步调用时主 Agent 阻塞在 `call_subagent`，主 Agent 无法写 `@子名 /stop` 到 db（主 Agent 不在跑 LLM 循环）。所以阶段一同步子 Agent 的 /stop **只能由双击停止按钮批量触发**，单个子 Agent 的 /stop 要等阶段二异步调用才能做（异步子 Agent 在后台跑，主 Agent 空闲可写 `@子名 /stop`）。

**阶段一交付**：
- 主 Agent 能给同步调用的子 Agent 补充上下文（次末插入）
- 双击停止按钮能停所有子 Agent（批量 /stop）
- 子 Agent 遇到歧义仍用现有"直接退出"机制（不问主，`ask_main_agent` 留到阶段二）

### 阶段二：异步调用 + 进度查看

范围：
- `allowAsync` frontmatter 标识
- MCP 工具 schema 加 `async_mode` 参数
- `handler.dispatch` 的 `chat-with-*` 分支改造（同步/异步分流）
- `_dispatch_async_subagent` + `_run_subagent_async`
- SubagentRegistry + SubagentMemoryContext
- 动态注入区列出后台子 Agent
- `check_subagent_progress` 工具
- `ask_main_agent` 工具注入给异步子 Agent
- 主 Agent 提示词异步调用说明

**阶段二交付**：主 Agent 可异步派子 Agent，子 Agent 后台跑，主 Agent 看进度、补充上下文、停子、收完成通知。子 Agent 遇到歧义能问主 Agent。

---

## 兼容性

- 现有 6 个子 Agent 默认 `allowAsync: false`，同步调用路径不变
- 现有 `call_subagent` 函数签名扩展（加 `memory_context` 可选参数），不破坏现有调用方
- 现有 `_stop_requested` 信号灯保留，语义改变（只对主 Agent），现有主 Agent 检查逻辑不变
- 现有 messages db schema 加 `role="subagent_msg"` 不影响现有消息（现有消息 role 是 user/assistant/tool 等）
- 现有 `_inject_dynamic_resources` 复用，加一段"后台子 Agent 清单"注入

---

## 已知限制（不阻塞本次交付）

1. **无进程级隔离**：子 Agent OOM/崩溃可能拖垮主进程（同进程架构限制）
2. **SSE 重连状态同步**：子 Agent 完成通知在 SSE 断连期间丢失，主 Agent 可能漏处理（现有架构通用限制）
3. **子 Agent 超时无强制取消**：子 Agent 卡死且 /stop 无响应时，只能等用户双击停止或重启
4. **ask_main_agent 无超时**：主 Agent 退出后子 Agent 永远阻塞（本次不实现超时）
5. **双重广播风险**：阶段二异步调用时，chat_queue 调 `_tidy_context_impl` 可能与子 Agent 完成通知产生序列问题（待实现时验证）

---

## 相关文件（预估改动）

### 阶段一
- `agent/subagent_supplement.py`（新建）— SubagentSupplementQueue + SubagentSupplementItem
- `agent/subagent.py` — `_run_agent_loop` 改造 `enable_supplement=True`、drain 自己的 queue、移除 `is_stop_requested()` 检查、`call_subagent` 签名扩展加 `supplement_queue` 可选参数
- `agent/runner.py` — `_stop_requested` 语义调整（只主 Agent）、双击停止触发批量 /stop、`_inject_dynamic_resources` 加 `role="subagent_msg"` 消息注入支持
- `agent/generic/agent_loop.py` — 主 Agent supplement queue 消费 `@主Agent` 消息、逐条回复约束（复用现有 drain_supplement，加 `@主Agent` 消息路由）
- `niu_api/compat.py` 或 `niu_api/chat.py` — db 监测程序（后台 asyncio task，轮询 `role="subagent_msg"` 消息路由）
- `agent/session.py` 或 `niu_api/session.py` — messages db 加 `role="subagent_msg"` 值支持（content 前缀编码目标/发送者名，零 schema 改动）
- `config/agents/niu.md` — 主 Agent 提示词加逐条回复约束 + 命名规则说明
- `ui/assistant/chat.html` — 双击停止按钮识别 + `role="subagent_msg"` 消息特殊样式渲染 + 单击后子 Agent 仍在跑的 UX 提示
- `ui/assistant/main.js` — 双击停止按钮识别（现有单击停止的扩展）

### 阶段二
- `agent/subagent.py` — `build_subagent_tool_schema` 加 `async_mode`、`_dispatch_async_subagent`、`_run_subagent_async`、`ask_main_agent` 工具注入、`call_subagent` 签名加 `memory_context` 参数、`_run_agent_loop` 加 memory_context 更新钩子（在 `chunk = next(gen)` 后检测 StreamEvent 类型提取请求/响应）
- `agent/handler.py` — `dispatch` 的 `chat-with-*` 分支改造（同步/异步分流）
- `agent/subagent_registry.py`（新建）— SubagentRegistry + RunningSubagent + SubagentMemoryContext（含 snapshot/update 加锁）
- `agent/generic/agent_loop.py` 或 `agent/runner.py` — `_inject_dynamic_resources` 加后台子 Agent 清单注入（含数量上限 5）
- `agent/tool_registry.py` 或 `agent/handler.py` — `check_subagent_progress` 工具注册（调 `memory_context.snapshot()` 读一致状态）
- `config/agents/*.md` — 6 个子 Agent frontmatter 加 `allowAsync`（默认 false，用户决定哪些设 true）
- `config/agents/niu.md` — 主 Agent 提示词加异步调用说明
- `tests/test_subagent_interaction.py`（新建）— 阶段一集成测试
- `tests/test_async_subagent.py`（新建）— 阶段二集成测试
