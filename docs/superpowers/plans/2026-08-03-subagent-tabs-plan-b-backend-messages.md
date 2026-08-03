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
- `niu_api/db_monitor.py` L126: `is_terminate = content.strip() == '/stop'`（注意 strip）
- `niu_api/db_monitor.py` L127-138: /stop 分支
- `niu_api/db_monitor.py` L140-167: sender=='主Agent' 的 set_answer 降级
- `niu_api/db_monitor.py` L169-176: 其他 sender 的 supplement_queue.push
- `agent/subagent_supplement.py` L18-36: SubagentSupplementQueue.push/drain
- `agent/subagent_registry.py` L42-95: SubagentRegistry.get / list_running
- `agent/ask_main_agent.py` L53-121: PendingAskRegistry.set_answer / cancel_pending_ask
- `agent/runner.py` L140-142: `enqueue_supplement` 函数

- [ ] **Step 1: 创建 route_to_subagent 公共函数**

`agent/route_to_subagent.py`:

```python
"""公共函数：路由消息到子 Agent。

提取自 db_monitor.route_message 的子 Agent 路由逻辑。
POST API 和 db_monitor 都调用此函数。

source='db_monitor': 保留原有全部行为（含孤儿回答推回主 Agent via enqueue_supplement）
source='post_api': 孤儿回答返回 not_found，不推回主 Agent
"""
from loguru import logger
from agent.subagent_registry import SubagentRegistry
from agent.ask_main_agent import get_pending_ask_registry


def route_to_subagent(target: str, sender: str, content: str, source: str = 'db_monitor') -> dict:
    """路由消息到子 Agent。

    Returns:
        {"status": "ok"|"error"|"not_found", "message": str}
    """
    # trailing 标点 strip（先 strip 再查找，简化原 db_monitor 的两步查找）
    target = target.rstrip('。，！？；：、.,!?;:')
    instance = SubagentRegistry.get(target)

    if instance is None:
        # 孤儿回答
        if source == 'post_api':
            return {"status": "not_found", "message": f"子 Agent {target} 不存在或已结束"}
        # db_monitor 场景：sender=='主Agent' 丢弃避免死循环，其他推回主 Agent
        if sender == '主Agent':
            logger.warning(f"[route] 主 Agent 回复孤儿 {target}，丢弃避免死循环")
            return {"status": "error", "message": "orphan discarded"}
        # 推回主 Agent（与原 db_monitor 一致，用 enqueue_supplement）
        from agent.runner import enqueue_supplement
        fallback = f"@主Agent [system] 目标子 Agent {target} 已不存在：{content}"
        enqueue_supplement(fallback)
        return {"status": "error", "message": "orphan forwarded to main agent"}

    sq = instance.supplement_queue

    # /stop 终止命令（注意 strip，与 db_monitor L126 一致）
    if content.strip() == '/stop':
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
- Modify: `niu_api/chat.py` (新增端点 + 扩展 running API)

**参考代码位置:**
- `niu_api/chat.py` L737-746: `/api/stop_all` POST 端点
- `niu_api/chat.py` L748-763: `/api/subagents/running` GET 端点
- `niu_api/chat.py` L20: `router = APIRouter(tags=['chat'])`
- `agent/subagent_registry.py` L22-42: RunningSubagent（state L40, started_at L34）

- [ ] **Step 1: 新增 POST 端点（含空内容验证 + 无冗余 TOCTOU 检查）**

在 `niu_api/chat.py` 中 `/api/subagents/running` 端点之后新增:

```python
from pydantic import BaseModel, Field

class SubagentMessage(BaseModel):
    content: str = Field(..., min_length=1)

@router.post("/api/subagents/{unique_name}/message")
async def send_subagent_message(unique_name: str, msg: SubagentMessage):
    """用户向子 Agent 发送消息（补充信息或回答 @user 提问）。"""
    from agent.route_to_subagent import route_to_subagent

    content = msg.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="消息内容不能为空")

    result = route_to_subagent(unique_name, sender='user', content=content, source='post_api')
    if result['status'] == 'not_found':
        raise HTTPException(status_code=404, detail=result['message'])
    return {"status": result['status'], "message": result['message']}
```

注意：不在 POST API 中预检查 SubagentRegistry.get（避免 TOCTOU 竞态），让 route_to_subagent 内部处理 not_found 并映射到 404。

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
- Modify: `agent/subagent_registry.py` (state 注释新增 waiting_for_user)
- Modify: `agent/generic/agent_loop.py` (新增 @user 拦截 + INTERCEPTED_ASK_USER 常量 + _FORMAT_ERROR_PROMPT 更新)
- Modify: `agent/subagent.py` (新增 _ask_user_impl 函数 + 提示词注入)

**参考代码位置:**
- `agent/ask_main_agent.py` L28-127: AskMainAgentFuture + PendingAskRegistry（含 _ask_terminated 机制）
- `agent/generic/agent_loop.py` L13: `_AT_NIU_PREFIX = '@niu-agent'`
- `agent/generic/agent_loop.py` L16-21: `_FORMAT_ERROR_PROMPT`
- `agent/generic/agent_loop.py` L24-28: 常量定义（INTERCEPTED/EXIT 等）
- `agent/generic/agent_loop.py` L94-186: `_intercept_at_prefix_content`
- `agent/generic/agent_loop.py` L147: @niu-agent 检测
- `agent/generic/agent_loop.py` L196: @end 检测
- `agent/generic/agent_loop.py` L724-728: 拦截层调用点（用 `interception_status, interception_payload`）
- `agent/subagent.py` L1037-1122: `_ask_main_agent_impl`（参考模板）
- `agent/subagent.py` L483-523: `build_subagent_system_segments`
- `agent/subagent.py` L70/93/524: `_SUBAGENT_ASK_GUIDE_TEMPLATE` / `_SUBAGENT_ASK_GUIDE_MARKER`

- [ ] **Step 1: 创建 AskUserFuture + UserAskRegistry（含 _ask_user_terminated 防死锁）**

`agent/ask_user.py`（对称于 `agent/ask_main_agent.py`，含 `_ask_user_terminated` 标志防死锁）:

```python
"""子 Agent 向用户提问的阻塞机制。

与 ask_main_agent.py 对称：
- AskMainAgentFuture → 主 Agent 回答（经 db_monitor 双链路路由）
- AskUserFuture → 用户回答（经 POST API 直接 set_answer）

同一子 Agent 同一时刻只能有一个 Future 挂起（ask 阻塞子 Agent 循环）。
_ask_user_terminated 标志防止 /stop 在 register 和 wait 之间到达的死锁。
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
        else:
            # future 不存在——设 _ask_user_terminated 标志防止后续 register 阻塞
            try:
                from agent.subagent_registry import SubagentRegistry
                instance = SubagentRegistry.get(unique_name)
                if instance is not None:
                    instance._ask_user_terminated = True
            except Exception:
                pass

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

- [ ] **Step 2: 新增 @user 常量 + INTERCEPTED_ASK_USER + _FORMAT_ERROR_PROMPT 更新**

`agent/generic/agent_loop.py`，在 L13 `_AT_NIU_PREFIX` 附近新增:
```python
_AT_USER_PREFIX = '@user'
```

在 L24-28 常量区域新增:
```python
INTERCEPTED_ASK_USER = 'intercepted_ask_user'
```

更新 `_FORMAT_ERROR_PROMPT`（L16-21），加入 @user 选项:
```python
_FORMAT_ERROR_PROMPT = (
    '[对话格式错误] 你的输出必须遵循以下格式之一：\n'
    f'1. 询问主 Agent：content 中包含 `{_AT_NIU_PREFIX}`，如 `{_AT_NIU_PREFIX} 需要更多上下文`\n'
    '2. 询问用户：content 中包含 `@user`，如 `@user 你需要哪个文件？`\n'
    '3. 结束会话：content 中包含 `@end`\n'
    '禁止输出不带 @ 前缀的纯 content。请重新输出。'
)
```

- [ ] **Step 3: _intercept_at_prefix_content 新增 @user 拦截（含词边界检查）**

在 `agent/generic/agent_loop.py` `_intercept_at_prefix_content` 函数中，@niu-agent 检测块之后、@end 检测之前，新增:

```python
# @user 检测（词边界检查，避免 @username 误匹配）
at_user_idx = _find_unescaped_marker(stripped, _AT_USER_PREFIX)
if at_user_idx >= 0:
    after_marker = at_user_idx + len(_AT_USER_PREFIX)
    # 检查 @user 后面是空白或字符串结尾（词边界）
    if after_marker >= len(stripped) or stripped[after_marker] in (' ', '\t', '\n'):
        question = stripped[after_marker:].strip()
        if not question:
            return (FORMAT_ERROR, '@user 后面必须跟问题内容')
        return (INTERCEPTED_ASK_USER, question)
    # @user 后面紧跟非空白字符（如 @username），不拦截，继续检测 @end
```

- [ ] **Step 4: agent_loop 中处理 INTERCEPTED_ASK_USER（提取 unique_name + 处理 None 回答）**

`agent/generic/agent_loop.py` L724-728 区域，使用与现有代码一致的变量名 `interception_status` / `interception_payload`:

```python
if interception_status == INTERCEPTED_ASK_USER:
    question = interception_payload
    unique_name = getattr(handler, '_subagent_unique_name', '')
    if not unique_name:
        continue  # 无 unique_name，跳过（不应发生）
    from agent.subagent import _ask_user_impl
    answer = _ask_user_impl(question, unique_name)
    if answer and answer != '__TERMINATED__':
        messages.append({"role": "user", "content": f"[user 回答] {answer}"})
    else:
        messages.append({"role": "user", "content": "[user 未回答] 你的提问超时或被终止，请基于现有信息继续或用 @end 退出。"})
    continue
```

注意：此分支插入在现有 `if interception_status == INTERCEPTED:` 之后、`if interception_status == INTERCEPTED_SYNC:` 之前。

- [ ] **Step 5: 新增 _ask_user_impl 函数（精简参数 + instance None 检查 + terminated 标志 + try/except ImportError）**

`agent/subagent.py`，在 `_ask_main_agent_impl` 之后新增。**注意：签名只用 (question, unique_name)，不用 handler/client**:

```python
def _ask_user_impl(question: str, unique_name: str) -> str | None:
    """子 Agent 向用户提问，阻塞等待用户回答。

    1. 推送 question 事件到 SubagentEventBus（前端 tab 高亮显示问题）
    2. 设 state=waiting_for_user
    3. 注册 AskUserFuture，阻塞等待
    4. 设置 state=running（仅在子 Agent 仍存活时）
    5. 返回用户回答
    """
    from agent.ask_user import get_user_ask_registry, TERMINATED_SIGNAL
    from agent.subagent_registry import SubagentRegistry

    instance = SubagentRegistry.get(unique_name)
    if instance is None:
        return '[ask_user 错误] 子 Agent 已不在注册表'

    # 检查 _ask_user_terminated 标志（/stop 在 register 之前到达）
    if getattr(instance, '_ask_user_terminated', False):
        return '[ask_user 已终止] 用户或系统已发出停止指令，请总结本轮工作后终止。'

    # 推送问题到前端
    try:
        from niu_api.internal.subagent_event_bus import notify_subagent_event_sync
        notify_subagent_event_sync(unique_name, 'question', {'content': question[:2000]})
    except ImportError:
        logger.warning('[ask_user] SubagentEventBus not available, question event not pushed to frontend')

    # 设 state
    instance.state = 'waiting_for_user'

    # 注册 future 并阻塞
    registry = get_user_ask_registry()
    future = registry.register(unique_name)
    try:
        answer = future.wait(timeout=600)
        if answer == TERMINATED_SIGNAL:
            return '[ask_user 已终止] 用户或系统已发出停止指令，请总结本轮工作后终止。'
        return answer
    finally:
        # 仅在子 Agent 仍存活时恢复 state
        if SubagentRegistry.get(unique_name) is instance:
            instance.state = 'running'
        registry.unregister(unique_name)
```

- [ ] **Step 6: 提示词注入 @user 语法（保留原常量名，更新内容 + marker）**

`agent/subagent.py`，**保留 `_SUBAGENT_ASK_GUIDE_TEMPLATE` 常量名不变**（避免改 L524 引用），只更新内容:

```python
_SUBAGENT_ASK_GUIDE_TEMPLATE = """<!-- NIU_SUBAGENT_GUIDE_v3 -->
## 通讯语法
- `@niu-agent 问题内容` — 向主 Agent 提问，阻塞等待回答（5 分钟超时）
- `@user 问题内容` — 向用户提问，阻塞等待回答（10 分钟超时）
- `@end` — 任务完成，退出
"""
```

更新 marker:
```python
_SUBAGENT_ASK_GUIDE_MARKER = "<!-- NIU_SUBAGENT_GUIDE_v3 -->"
```

L524 的引用 `_SUBAGENT_ASK_GUIDE_MARKER not in static_system` 和 L525 的 `static_system += '\n\n' + _SUBAGENT_ASK_GUIDE_TEMPLATE` **不需要改动**（常量名未变）。

- [ ] **Step 7: 语法检查**

```bash
python/bin/python -c "import ast; ast.parse(open('agent/ask_user.py').read()); ast.parse(open('agent/generic/agent_loop.py').read()); ast.parse(open('agent/subagent.py').read()); print('OK')"
```

- [ ] **Step 8: 提交**

```bash
git add agent/ask_user.py agent/subagent_registry.py agent/generic/agent_loop.py agent/subagent.py
git commit -m "feat: @user mechanism — AskUserFuture + UserAskRegistry + ask_user intercept + _ask_user_terminated + word boundary check"
```
