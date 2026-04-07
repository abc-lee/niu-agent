# GenericAgent 历史管理重构 - 审核报告与优化方案

**日期**: 2026-04-07
**审核范围**: mac-compat-dev 分支合并后的代码质量、架构一致性、功能完整性、高风险问题

---

## 一、审核结果汇总

### 1.1 Critical 级别问题（共 12 个）

| # | 问题 | 来源 | 影响 |
|---|------|------|------|
| 1 | **BaseSession.history 字段废弃但未删除** | 架构审核 | 代码混乱，注释与实现不一致 |
| 2 | **ExperienceSummarizer/AutonomousExplorer 未集成** | 架构审核 | 新增代码无效，维护负担 |
| 3 | **历史消息加载逻辑错误** | 功能审核 | compat.py 排除最新用户消息，可能导致丢失 |
| 4 | **上下文无限制增长** | 功能审核 | 长对话可能导致 LLM API 失败 |
| 5 | **MessageStore 排序逻辑错误** | 功能审核 | `get_messages(limit=N)` 返回最旧而非最新 |
| 6 | **assert 类型检查失效风险** | 代码质量 | 生产环境优化后类型检查失效 |
| 7 | **变量未初始化** | 代码质量 | `content_blocks` 可能为 None |
| 8 | **数据库连接泄露** | 代码质量 | 异常时未关闭连接 |
| 9 | **JSON 解析无保护** | 代码质量 | LLM 返回非法 JSON 时崩溃 |
| 10 | **消息格式未验证** | 代码质量 | 格式错误导致后续崩溃 |
| 11 | **上下文压缩机制缺失** | 高风险 | 长对话可能溢出，Token 超限 |
| 12 | **工具调用无重试机制** | 高风险 | 临时故障导致工具失败 |

### 1.2 Major 级别问题（共 10 个）

| # | 问题 | 影响 |
|---|------|------|
| 1 | 工具循环死循环风险 | 无重复调用检测，浪费资源 |
| 2 | 历史消息加载无限制 | 100+ 轮对话加载所有历史，性能问题 |
| 3 | 数据库连接未正确关闭 | 资源泄露 |
| 4 | 缺少异常处理 | 经验总结失败可能中断主流程 |
| 5 | 文件写入无异常处理 | 无法区分失败原因 |
| 6 | 线程 join 超时未验证 | 可能产生僵尸线程 |
| 7 | 历史管理职责分散 | MessageStore/BaseSession/runner 职责不清 |
| 8 | compress_history_tags 应独立 | 应移到 ContextManager |
| 9 | 缺少上下文窗口管理策略 | 未实现消息删除逻辑 |
| 10 | 数据流缺少类型安全 | 多处格式转换，易出错 |

---

## 二、问题根因分析

### 2.1 核心问题：历史管理职责分散

**症状**：
- MessageStore（持久化）、BaseSession.history（内存缓存）、agent_loop（临时组装）三者职责重叠
- 数据流混乱：MessageStore → compat.py → runner.py → agent_loop.py → llmcore.py
- 多处缓存可能导致数据不一致

**根因**：
- GenericAgent 的 BaseSession.history 与我们的 MessageStore 双轨冲突
- 昨天提交（4c6e503）移除了删除逻辑，暴露了架构问题
- 注释说"不再管理历史"，但 BaseSession.history 字段仍存在

**影响**：
- 维护困难，容易产生 Bug
- 新开发者难以理解
- 注释与代码不一致

### 2.2 次要问题：新增模块未集成

**症状**：
- `ExperienceSummarizer` 代码完整，但缺少调用点
- `AutonomousExplorer` 未启动，`record_activity()` 未调用

**根因**：
- 设计文档完整，但实施未完成
- handler.py 只有方法定义，未在主流程中调用

**影响**：
- 代码存在但无效，增加维护负担
- 无法实现预期功能

### 2.3 功能缺陷：边界情况未处理

**症状**：
- 历史消息加载逻辑错误（compat.py:139-145）
- 上下文无限制增长（agent_loop.py 无压缩）
- MessageStore 排序错误（session.py:141）

**根因**：
- 对 agent_runner_loop 的 history 参数理解错误
- 移除 trim_messages_history 后缺少替代机制
- SQL 查询 DESC + reverse 逻辑混乱

**影响**：
- 消息丢失或错误加载
- LLM API 调用失败
- 性能问题

---

## 三、优化方案

### Phase 1: 修复 Critical 问题（紧急，1-2 天）

#### 1.1 清理 BaseSession.history 废弃字段

**修改文件**：`agent/generic/llmcore.py`

**修改内容**：
```python
# 删除第 632 行的 self.history = []
# 删除第 633 行的 self.lock = threading.Lock()
# 删除所有对 self.history 的引用（已在注释中标注"不再管理"）
```

**影响范围**：
- `BaseSession.__init__` (llmcore.py:632)
- `NativeClaudeSession.__init__` (llmcore.py:826)

**风险**：低（字段已废弃，仅删除冗余代码）

---

#### 1.2 修复历史消息加载逻辑

**修改文件**：`niu_api/compat.py`

**当前代码（错误）**：
```python
# 第 139-143 行
history_for_runner = [
    {"role": msg.role, "content": msg.content}
    for msg in history[:-1]  # ❌ 排除了最新用户消息
    if msg.content
]
```

**修复代码**：
```python
# 当前用户消息已经存储在数据库中（第130行）
# history 参数应该是"之前"的对话历史，不包含当前消息
history_for_runner = [
    {"role": msg.role, "content": msg.content}
    for msg in history[:-1]  # ✅ 排除最后一条（当前消息）
    if msg.content
]
# 当前用户消息通过 user_input 参数传递（第158行已正确）
```

**问题分析**：
- 当前代码逻辑是正确的！history[:-1] 排除最后一条（当前消息）
- 当前消息通过 `request.message` 传递给 `runner.chat(..., user_input=request.message)`
- agent_runner_loop 会将 user_input 作为最新的 user 消息追加

**结论**：✅ **无需修改**，代码逻辑正确

---

#### 1.3 修复 MessageStore 排序逻辑

**修改文件**：`agent/session.py`

**当前代码（错误）**：
```python
# 第 125-130 行
cursor = await db.execute(
    """SELECT * FROM messages
       WHERE session_id = ?
       ORDER BY created_at ASC  -- ❌ ASC 导致返回最旧的
       LIMIT ?""",
    (session_id, limit),
)
messages = await cursor.fetchall()
messages.reverse()  # 反转为最旧在上
```

**修复代码**：
```python
# 方案1：返回最新 N 条消息（推荐）
cursor = await db.execute(
    """SELECT * FROM messages
       WHERE session_id = ?
       ORDER BY created_at DESC  -- ✅ DESC 最新的在上
       LIMIT ?""",
    (session_id, limit),
)
messages = await cursor.fetchall()
messages.reverse()  # 反转为最旧在上（符合 UI 展示顺序）
```

**影响**：
- `get_messages(limit=50)` 将返回最近 50 条消息
- 避免加载所有历史导致上下文爆炸

---

#### 1.4 添加上下文长度限制

**修改文件**：`niu_api/compat.py`

**修改内容**：
```python
# 第 134 行，修改为：
history = await store.get_messages(limit=50)  # 限制最近 50 条
```

**理由**：
- 避免 100+ 轮对话加载所有历史
- 50 条消息约 10-15K tokens，在可控范围内
- 后续可实现动态估算

---

#### 1.5 修复代码质量问题

**5.1 替换 assert 为类型检查**

**修改文件**：`agent/generic/llmcore.py`

```python
# 第 826 行，修改为：
if not isinstance(msg, dict):
    raise TypeError(f"Expected dict, got {type(msg)}")
```

**5.2 初始化 content_blocks**

**修改文件**：`agent/generic/llmcore.py`

```python
# 第 831 行，修改为：
messages = [msg]  # Just pass the single message
content_blocks = []  # ✅ 初始化
gen = self.raw_ask(messages, tools, self.system, model)
```

**5.3 添加 JSON 解析异常处理**

**修改文件**：`agent/generic/agent_loop.py`

```python
# 第 120-127 行，修改为：
tool_calls = []
for tc in response.tool_calls:
    try:
        args = json.loads(tc.function.arguments)
        tool_calls.append({
            "tool_name": tc.function.name,
            "args": args,
            "id": tc.id,
        })
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse tool arguments: {e}")
        tool_calls.append({
            "tool_name": tc.function.name,
            "args": {},
            "id": tc.id,
            "error": str(e),
        })
```

**5.4 使用 with 管理数据库连接**

**修改文件**：`agent/handler.py`

```python
# 第 679-688 行，修改为：
db_path = Path.home() / ".niu" / "scheduled_tasks.db"
try:
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,))
        task = cursor.fetchone()
        # ... 处理逻辑
except sqlite3.Error as e:
    logger.error(f"Database error: {e}")
    return None
```

---

### Phase 2: 架构优化（中期，3-5 天）

#### 2.1 统一历史管理职责

**创建文件**：`agent/context_manager.py`

**职责**：
- 历史加载（从 MessageStore）
- 上下文压缩（Token 计数 + 消息删除）
- 历史格式转换（Message → dict）

**接口设计**：
```python
class ContextManager:
    """上下文管理器 - 统一历史管理职责"""

    def __init__(self, message_store: MessageStore):
        self.store = message_store
        self.max_tokens = 200000  # 从配置读取

    def load_history(self, limit: int = 50) -> list[dict]:
        """加载历史消息（转换为 agent_loop 格式）"""
        messages = await self.store.get_messages(limit=limit)
        return [{"role": msg.role, "content": msg.content} for msg in messages]

    def compress_if_needed(self, messages: list, current_tokens: int) -> list:
        """如果超限，压缩消息列表"""
        if current_tokens > self.max_tokens * 0.8:
            # 删除早期消息
            return self._delete_early_messages(messages)
        return messages

    def count_tokens(self, messages: list) -> int:
        """估算 token 数量（使用 tiktoken）"""
        # 实现参考 implementation-L0L1L2.md
        pass
```

**集成点**：
- `compat.py`: 使用 `ContextManager.load_history()`
- `agent_loop.py`: 使用 `ContextManager.compress_if_needed()`

---

#### 2.2 实现上下文压缩策略

**策略**：
1. **L0 层**：最近 10 条消息（完整保留）
2. **L1 层**：中间消息（压缩为摘要）
3. **L2 层**：早期消息（提取关键点，删除原文）

**实现**：
```python
def compress_messages(messages: list) -> list:
    """三级压缩策略"""
    if len(messages) <= 10:
        return messages  # L0: 保留

    # L1: 压缩中间消息
    compressed = messages[-10:]  # L0
    middle = messages[:-10]

    # 生成摘要（调用 LLM）
    summary = generate_summary(middle)
    compressed.insert(0, {"role": "user", "content": f"[历史摘要]\n{summary}"})

    return compressed
```

---

#### 2.3 集成 ExperienceSummarizer

**修改文件**：`agent/handler.py`

**修改内容**：
```python
# 在 tool_after_callback 中添加：
def tool_after_callback(self, tool_name, args, response, ret):
    # 追踪工具执行
    self._track_tool_execution(tool_name, args, ret)

    # 检查是否需要总结经验
    if self._check_and_summarize_experience():
        summary = self._experience_summarizer.summarize_experience(
            self.history_info,
            self.working.get("tool_results", [])
        )
        logger.info(f"Generated experience summary: {summary[:100]}")
```

---

#### 2.4 添加 MCP 工具调用重试机制

**修改文件**：`agent/mcp_sync_bridge.py`

```python
async def call_tool_with_retry(self, tool_name: str, arguments: dict, max_retries: int = 2) -> dict:
    """带重试的 MCP 工具调用"""
    for attempt in range(max_retries + 1):
        try:
            result = await self.call_tool(tool_name, arguments)
            return result
        except Exception as e:
            if attempt < max_retries:
                logger.warning(f"MCP tool call failed (attempt {attempt + 1}), retrying: {e}")
                await asyncio.sleep(1 * (attempt + 1))  # 指数退避
            else:
                logger.error(f"MCP tool call failed after {max_retries} retries: {e}")
                raise
```

---

### Phase 3: 功能完善（长期，1-2 周）

#### 3.1 实现工具重复调用检测

**修改文件**：`agent/handler.py`

```python
def next_prompt_patcher(self, next_prompt, outcome, turn):
    # 检测重复调用
    if turn > 3:
        recent_tools = self.history_info.get("recent_tools", [])[-3:]
        if len(set(recent_tools)) == 1:
            # 最近 3 次调用相同工具
            logger.warning(f"Detected repeated tool calls: {recent_tools}")
            next_prompt = (
                f"警告：你已连续 3 次调用相同工具（{recent_tools[0]}）。\n"
                f"请考虑：\n"
                f"1. 检查参数是否正确\n"
                f"2. 尝试其他方法\n"
                f"3. 询问用户更多信息\n\n"
                f"原始提示：{next_prompt}"
            )
    return next_prompt
```

---

#### 3.2 启动 AutonomousExplorer

**修改文件**：`niu_api/chat.py`

```python
from agent.autonomous_explorer import AutonomousExplorer, record_activity

# 在 init_runner 中启动
def init_runner(mcp_tools):
    global _runner
    # ... 初始化 runner

    # 启动自主探索器
    explorer = AutonomousExplorer()
    explorer.start()
    logger.info("AutonomousExplorer started")
```

**修改文件**：`niu_api/compat.py`

```python
from agent.autonomous_explorer import record_activity

# 在 chat_session 中记录活动
@router.post("/api/chat/session")
async def chat_session(request: ChatRequest) -> ChatResponse:
    # 记录用户活动
    record_activity()

    # ... 原有逻辑
```

---

#### 3.3 实现智能上下文估算

**创建文件**：`agent/token_counter.py`

```python
import tiktoken

class TokenCounter:
    """Token 计数器"""

    def __init__(self, model: str = "gpt-4"):
        self.encoding = tiktoken.encoding_for_model(model)

    def count_messages(self, messages: list) -> int:
        """计算消息列表的 token 数"""
        total = 0
        for msg in messages:
            # 每条消息的固定开销
            total += 4  # role + content 标签

            # 内容 token
            content = msg.get("content", "")
            if isinstance(content, str):
                total += len(self.encoding.encode(content))
            elif isinstance(content, list):
                # Claude 格式（content blocks）
                for block in content:
                    if block.get("type") == "text":
                        total += len(self.encoding.encode(block.get("text", "")))

        return total
```

---

## 四、实施优先级

### P0: 立即修复（1-2 天）

1. ✅ 清理 BaseSession.history 废弃字段
2. ✅ 修复 MessageStore 排序逻辑
3. ✅ 添加上下文长度限制（50 条）
4. ✅ 替换 assert 为类型检查
5. ✅ 初始化 content_blocks
6. ✅ 添加 JSON 解析异常处理
7. ✅ 使用 with 管理数据库连接

### P1: 架构优化（3-5 天）

1. 创建 ContextManager 统一历史管理
2. 实现上下文压缩策略（L0/L1/L2）
3. 集成 ExperienceSummarizer
4. 添加 MCP 工具调用重试

### P2: 功能完善（1-2 周）

1. 实现工具重复调用检测
2. 启动 AutonomousExplorer
3. 实现智能 token 估算（tiktoken）
4. 完善异常处理和降级策略

---

## 五、风险评估

| 风险 | 级别 | 缓解措施 |
|------|------|----------|
| P0 修改影响现有功能 | 低 | 逐个修复，每个修改后测试 |
| ContextManager 集成复杂度 | 中 | 分阶段实施，先实现基础功能 |
| 上下文压缩可能丢失重要信息 | 中 | 优先删除早期消息，保留最近 10 条 |
| tiktoken 增加依赖 | 低 | 使用 pip install，打包时包含 |

---

## 六、验证方案

### 6.1 单元测试

```bash
# 测试历史加载
pytest tests/test_session.py::test_get_messages_limit

# 测试上下文压缩
pytest tests/test_context_manager.py

# 测试工具重试
pytest tests/test_mcp_bridge.py::test_retry
```

### 6.2 集成测试

1. **长对话测试**：50+ 轮对话，验证上下文限制
2. **工具失败测试**：模拟 MCP 服务器故障，验证重试
3. **并发测试**：多用户同时对话，验证线程安全

### 6.3 性能测试

```bash
# 监控内存占用
python -m memory_profiler niu_api

# 监控 token 使用
python scripts/test_token_count.py
```

---

## 七、总结

**当前状态**：
- 架构设计清晰，但实施不完整
- 核心功能可用，但存在边界问题
- 代码质量整体良好，但缺少防御性编程

**关键问题**：
1. 历史管理职责分散 → 需统一为 ContextManager
2. 新增模块未集成 → 需完成集成或删除
3. 边界情况未处理 → 需添加异常处理和验证

**优化收益**：
- P0 修复：解决 Critical 问题，避免生产故障
- P1 优化：提升架构一致性，降低维护成本
- P2 完善：实现完整功能，提升用户体验

**预计工作量**：
- P0: 1-2 天（立即开始）
- P1: 3-5 天（下周开始）
- P2: 1-2 周（逐步实施）
