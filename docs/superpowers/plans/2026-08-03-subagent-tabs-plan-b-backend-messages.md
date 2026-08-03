# 动态子 Agent 标签页 — 计划 B：后端消息通道 + @user 机制

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户可以向前端子 Agent tab 中的子 Agent 发送补充信息；子 Agent 遇到问题可通过 `@user` 向用户提问，阻塞等待用户回答。

**Architecture:** 提取 `db_monitor.route_message` 核心逻辑为 `route_to_subagent` 公共函数，新增 POST API 端点复用它。新建 `AskUserFuture` + `UserAskRegistry`（对称于 `AskMainAgentFuture` + `PendingAskRegistry`），子 Agent 输出 `@user` 前缀时拦截并阻塞等待，用户通过 POST API 唤醒。

**Tech Stack:** Python, FastAPI, threading, loguru

**设计文档:** `docs/superpowers/specs/2026-08-03-dynamic-subagent-tabs-design.md` §4.4, §4.6

**依赖:** 计划 A（SubagentEventBus）已完成

---

### Task 1: 提取 route_to_subagent 公共函数

**Files:**
- Create: `agent/route_to_subagent.py`
- Modify: `niu_api/db_monitor.py` L75-176 (route_message 函数)

**参考代码位置:**
- `niu_api/db_monitor.py` L75-176: `route_message` 完整实现
- `niu_api/db_monitor.py` L104-111: trailing 标点 strip + 二次查找
- `niu_api/db_monitor.py` L113-124: 孤儿回答处理（instance is None）
- `niu_api/db_monitor.py` L127-138: /stop 分支（cancel_pending_ask + push is_terminate）
- `niu_api/db_monitor.py` L140-167: sender=='主Agent' 的 set_answer 降级
- `niu_api/db_monitor.py` L169-176: 其他 sender 的 supplement_queue.push
- `agent/subagent_supplement.py` L18-36: SubagentSupplementQueue.push/drain
- `agent/subagent_registry.py` L42-95: SubagentRegistry.get / list_running
- `agent/ask_main_agent.py` L53-121: PendingAskRegistry.set_answer / cancel_pending_ask

- [ ] **Step 1: 创建 route_to_subagent 公共函数**

`agent/route_to_subagent.py`:

```python
"""公共函数：路由消息到子 Agent。

提取自 db_monitor.route_message 的子 Agent 路由逻辑。
POST API 和 db_monitor 都调用此函数。

source='db_monitor': 保留原有全部行为（含孤儿回答推回主 Agent）
source='post_api': 孤儿回答返回 404，不推回主 Agent
"""
from loguru import logger
from agent.subagent_registry import SubagentRegistry
from agent.ask_main_agent import get_pending_ask_registry


def route_to_subagent(target: str, sender: str, content: str, source: str = 'db_monitor') -> dict:
    """路由消息到子 Agent。

    Returns:
        {"status": "ok"|"error"|"not_found", "message": str}
    """
    # trailing 标点 strip
    target = target.rstrip('。，！？；：、.,!?;:')
    instance = SubagentRegistry.get(target)

    if instance is None:
        # 孤儿回答
        if source == 'post_api':
            return {"status": "not_found", "message": f"子 Agent {target} 不存在或已结束"}
        # db_monitor 场景：sender=='主Agent' 丢弃，其他推回主 Agent
        if sender == '主Agent':
            logger.warning(f"[route] 主 Agent 回复孤儿 {target}，丢弃避免死循环")
            return {"status": "error", "message": "orphan discarded"}
        # 推回主 Agent（db_monitor 特有行为）
        from agent.main_agent_request_queue import get_main_agent_request_queue
        get_main_agent_request_queue().push(f"@主Agent [系统转发] 子Agent {target} 不存在，无法投递消息：{content}")
        return {"status": "error", "message": "orphan forwarded to main agent"}

    sq = instance.supplement_queue

    # /stop 终止命令
    if content == '/stop':
        pending_ask = get_pending_ask_registry()
        pending_ask.cancel_pending_ask(target)
        # 也 cancel pending ask_user（如果有）
        try:
            from agent.ask_user import get_user_ask_registry
            get_user_ask_registry().cancel_pending_ask(target)
        except ImportError:
            pass
        sq.push('/stop', is_terminate=True, sender=sender)
        logger.info(f"[route] /stop → {target}")
        return {"status": "ok", "message": f"已发送 /stop 到 {target}"}

    # 主 Agent 回复 @niu-agent 挂起的子 Agent
    if sender == '主Agent':
        pending_ask = get_pending_ask_registry()
        if pending_ask.set_answer(target, content):
            logger.info(f"[route] 主 Agent 回答 → {target}")
            return {"status": "ok", "message": f"已回答 {target}"}
        # set_answer 失败（无 pending future），降级推 supplement_queue
        logger.warning(f"[route] {target} 无 pending ask，降级推 supplement_queue")
        sq.push(content, is_terminate=False, sender=sender)
        return {"status": "ok", "message": f"已推送补充信息到 {target}"}

    # 用户回答 @user 挂起的子 Agent
    if sender == 'user':
        try:
            from agent.ask_user import get_user_ask_registry
            user_ask = get_user_ask_registry()
            if user_ask.set_answer(target, content):
                logger.info(f"[route] 用户回答 → {target}")
                return {"status": "ok", "message": f"已回答 {target}"}
        except ImportError:
            pass
        # 无 pending ask_user 或模块未就绪，降级推 supplement_queue
        sq.push(content, is_terminate=False, sender='user')
        return {"status": "ok", "message": f"已推送补充信息到 {target}"}

    # 其他 sender：直接推 supplement_queue
    sq.push(content, is_terminate=False, sender=sender)
    return {"status": "ok", "message": f"已推送消息到 {target}"}
```

- [ ] **Step 2: db_monitor.route_message 改为调用 route_to_subagent**

`niu_api/db_monitor.py` L127-176 区域（target==子名的分支），替换为:
```python
from agent.route_to_subagent import route_to_subagent
result = route_to_subagent(target, sender, content, source='db_monitor')
logger.info(f"[db_monitor] route_to_subagent: {result}")
```
保留 L88-107 的 target=='主Agent' 分支不变（那是 db_monitor 特有的推 MainAgentRequestQueue 逻辑）。

- [ ] **Step 3: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('agent/route_to_subagent.py').read()); ast.parse(open('niu_api/db_monitor.py').read()); print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add agent/route_to_subagent.py niu_api/db_monitor.py
git commit -m "refactor: extract route_to_subagent public function from db_monitor"
```

---

### Task 2: 新增 POST /api/subagents/{unique_name}/message API

**Files:**
- Modify: `niu_api/chat.py` (新增端点，参考 L737-763 的 /api/subagents/running 和 /api/stop_all)

**参考代码位置:**
- `niu_api/chat.py` L737-746: `/api/stop_all` POST 端点
- `niu_api/chat.py` L748-763: `/api/subagents/running` GET 端点
- `niu_api/chat.py` L20: `router = APIRouter(tags=['chat'])`

- [ ] **Step 1: 新增 POST 端点**

在 `niu_api/chat.py` 中 `/api/subagents/running` 端点之后新增:

```python
from pydantic import BaseModel

class SubagentMessage(BaseModel):
    content: str

@router.post("/api/subagents/{unique_name}/message")
async def send_subagent_message(unique_name: str, msg: SubagentMessage):
    """用户向子 Agent 发送消息（补充信息或回答 @user 提问）。"""
    from agent.route_to_subagent import route_to_subagent
    from agent.subagent_registry import SubagentRegistry

    instance = SubagentRegistry.get(unique_name)
    if instance is None:
        raise HTTPException(status_code=404, detail=f"子 Agent {unique_name} 不存在或已结束")

    result = route_to_subagent(unique_name, sender='user', content=msg.content, source='post_api')
    return {"status": result["status"], "message": result["message"]}
```

- [ ] **Step 2: 扩展 /api/subagents/running 返回 state + started_at**

`niu_api/chat.py` L748-763，在返回的 subagent dict 中增加 `state` 和 `started_at`:
```python
subagents_data.append({
    "unique_name": inst.unique_name,
    "agent_type": inst.agent_type,
    "is_sync": inst.is_sync,
    "state": getattr(inst, 'state', 'running'),
    "started_at": getattr(inst, 'started_at', None),
})
```

- [ ] **Step 3: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('niu_api/chat.py').read()); print('OK')"
```

- [ ] **Step 4: 提交**

```bash
git add niu_api/chat.py
git commit -m "feat: POST /api/subagents/{unique_name}/message + extend running API with state/started_at"
```

---

### Task 3: AskUserFuture + UserAskRegistry — 子 Agent 向用户提问

**Files:**
- Create: `agent/ask_user.py`
- Modify: `agent/subagent_registry.py` L22-42 (RunningSubagent 新增 waiting_for_user state)
- Modify: `agent/generic/agent_loop.py` L94-186 (_intercept_at_prefix_content — 新增 @user 拦截)
- Modify: `agent/subagent.py` (新增 _ask_user_impl 函数)
- Modify: `agent/subagent.py` L483-523 (build_subagent_system_segments — 注入 @user 语法说明)

**参考代码位置:**
- `agent/ask_main_agent.py` L28: `TERMINATED_SIGNAL = '__TERMINATED__'`
- `agent/ask_main_agent.py` L31-48: `AskMainAgentFuture` 类（threading.Event + _answer 共享变量）
- `agent/ask_main_agent.py` L53-121: `PendingAskRegistry` 类（register/set_answer/cancel_pending_ask/unregister）
- `agent/ask_main_agent.py` L124-127: `get_pending_ask_registry()` 全局单例
- `agent/generic/agent_loop.py` L94-186: `_intercept_at_prefix_content` — @niu-agent / @end 前缀拦截
- `agent/generic/agent_loop.py` L13: `_AT_NIU_PREFIX = '@niu-agent'`
- `agent/subagent.py` L1037-1122: `_ask_main_agent_impl` — ask_main_agent 工具实现（阻塞 + future.wait(300)）
- `agent/subagent.py` L483-523: `build_subagent_system_segments` — 强制注入 @niu-agent/@end 守则

- [ ] **Step 1: 创建 AskUserFuture + UserAskRegistry**

`agent/ask_user.py`（对称于 `agent/ask_main_agent.py`，但更简单——不经 db_monitor）:

```python
"""子 Agent 向用户提问的阻塞机制。

与 ask_main_agent.py 对称：
- AskMainAgentFuture → 主 Agent 回答（经 db_monitor 双链路路由）
- AskUserFuture → 用户回答（经 POST API 直接 set_answer）

同一子 Agent 同一时刻只能有一个 Future 挂起（ask 阻塞子 Agent 循环）。
"""
import threading
from loguru import logger

TERMINATED_SIGNAL = '__TERMINATED__'
_ASK_TIMEOUT = 600  # 10 分钟（比 ask_main_agent 的 300s 长，用户响应慢）


class AskUserFuture:
    """子 Agent 向用户提问的 future，阻塞等待用户回答。"""

    def __init__(self):
        self._event = threading.Event()
        self._answer = None

    def set_answer(self, answer: str):
        self._answer = answer
        self._event.set()

    def wait(self, timeout: float = _ASK_TIMEOUT) -> str | None:
        if self._event.wait(timeout=timeout):
            return self._answer
        return None  # 超时


class UserAskRegistry:
    """管理 unique_name → AskUserFuture。"""

    def __init__(self):
        self._futures: dict[str, AskUserFuture] = {}
        self._lock = threading.Lock()

    def register(self, unique_name: str) -> AskUserFuture:
        with self._lock:
            # 旧 future 设 TERMINATED 解除（防泄漏）
            old = self._futures.get(unique_name)
            if old is not None:
                old.set_answer(TERMINATED_SIGNAL)
            future = AskUserFuture()
            self._futures[unique_name] = future
            return future

    def set_answer(self, unique_name: str, answer: str) -> bool:
        with self._lock:
            future = self._futures.pop(unique_name, None)
            if future is None:
                return False
            future.set_answer(answer)
            return True

    def cancel_pending_ask(self, unique_name: str):
        with self._lock:
            future = self._futures.pop(unique_name, None)
            if future is not None:
                future.set_answer(TERMINATED_SIGNAL)

    def unregister(self, unique_name: str):
        with self._lock:
            self._futures.pop(unique_name, None)

    def is_waiting(self, unique_name: str) -> bool:
        with self._lock:
            return unique_name in self._futures


_user_ask_registry = UserAskRegistry()


def get_user_ask_registry() -> UserAskRegistry:
    return _user_ask_registry
```

- [ ] **Step 2: RunningSubagent state 新增 waiting_for_user**

`agent/subagent_registry.py` L22-42，在 state 字段注释中新增 `'waiting_for_user'` 值说明。不需要改代码（state 是 str 字段，不限制值）。

- [ ] **Step 3: _intercept_at_prefix_content 新增 @user 拦截**

`agent/generic/agent_loop.py` L94-186，在 `@niu-agent` 检测之后、`@end` 检测之前，新增 `@user` 检测:

```python
_AT_USER_PREFIX = '@user'

# 在 _intercept_at_prefix_content 函数中，@niu-agent 检测之后：
if _find_unescaped_marker(stripped, _AT_USER_PREFIX):
    question = stripped[len(_AT_USER_PREFIX):].strip()
    if not question:
        return ('FORMAT_ERROR', '@user 后面必须跟问题内容')
    return ('INTERCEPTED_ASK_USER', question)
```

- [ ] **Step 4: agent_loop 中处理 INTERCEPTED_ASK_USER**

`agent/generic/agent_loop.py` L724-728（拦截层调用点），新增分支:
```python
elif interception_result[0] == 'INTERCEPTED_ASK_USER':
    question = interception_result[1]
    # 调 _ask_user_impl 阻塞等待用户回答
    from agent.subagent import _ask_user_impl
    answer = _ask_user_impl(question, unique_name, handler, client)
    if answer:
        messages.append({"role": "user", "content": f"[user 回答] {answer}"})
    continue  # 继续下一轮 LLM
```

- [ ] **Step 5: 新增 _ask_user_impl 函数**

`agent/subagent.py`，在 `_ask_main_agent_impl` 之后新增:

```python
def _ask_user_impl(question: str, unique_name: str, handler, client) -> str | None:
    """子 Agent 向用户提问，阻塞等待用户回答。

    1. 推送 question 事件到 SubagentEventBus（前端 tab 高亮显示问题）
    2. 设 state=waiting_for_user
    3. 注册 AskUserFuture，阻塞等待
    4. 设置 state=running
    5. 返回用户回答
    """
    from agent.ask_user import get_user_ask_registry, TERMINATED_SIGNAL
    from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
    from agent.subagent_registry import SubagentRegistry

    # 推送问题到前端
    notify_subagent_event_sync(unique_name, 'question', {'content': question[:2000]})

    # 设 state
    instance = SubagentRegistry.get(unique_name)
    if instance:
        instance.state = 'waiting_for_user'

    # 注册 future 并阻塞
    registry = get_user_ask_registry()
    future = registry.register(unique_name)
    try:
        answer = future.wait(timeout=600)
        if answer == TERMINATED_SIGNAL:
            return None  # 被 /stop 终止
        return answer
    finally:
        if instance:
            instance.state = 'running'
        registry.unregister(unique_name)
```

- [ ] **Step 6: build_subagent_system_segments 注入 @user 语法说明**

`agent/subagent.py` L483-523，在 `_SUBAGENT_ASK_GUIDE_TEMPLATE` 中追加 @user 语法:

```python
_SUBAGENT_ASK_GUIDE_TEMPLATE_V3 = """<!-- NIU_SUBAGENT_GUIDE_v3 -->
## 通讯语法
- `@niu-agent 问题内容` — 向主 Agent 提问，阻塞等待回答（5 分钟超时）
- `@user 问题内容` — 向用户提问，阻塞等待回答（10 分钟超时）
- `@end` — 任务完成，退出
"""
```
替换原 `_SUBAGENT_ASK_GUIDE_TEMPLATE`（更新 marker 版本号 v2→v3）。

- [ ] **Step 7: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('agent/ask_user.py').read()); ast.parse(open('agent/generic/agent_loop.py').read()); ast.parse(open('agent/subagent.py').read()); print('OK')"
```

- [ ] **Step 8: 提交**

```bash
git add agent/ask_user.py agent/subagent_registry.py agent/generic/agent_loop.py agent/subagent.py
git commit -m "feat: @user mechanism — AskUserFuture + UserAskRegistry + ask_user intercept"
```
