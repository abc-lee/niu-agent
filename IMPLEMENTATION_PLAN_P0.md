# P0 修复实施方案

## 一、修改清单

### 1. 清理 BaseSession.history 废弃字段

**文件**: `E:\tools\ai-bot\agent\generic\llmcore.py`

**修改位置**: 第 632-633 行

**当前代码**:
```python
632        self.history = []
633        self.lock = threading.Lock()
```

**修复代码**:
```python
# 删除这两行，因为 BaseSession 不再管理历史
# 历史管理已迁移到 MessageStore (agent/session.py)
```

**影响范围**:
- 仅影响 `BaseSession.__init__` 方法
- 无其他代码引用这两个字段（已验证）

**风险**: 低
- 字段已废弃，仅删除冗余代码
- 注释已说明"不再管理历史"

**测试方法**:
- 运行现有测试套件，确保无引用错误
- 启动服务，验证基本对话功能正常

---

### 2. 修复 MessageStore 排序逻辑

**文件**: `E:\tools\ai-bot\agent\session.py`

**修改位置**: 第 125-141 行

**当前代码**:
```python
125                if limit is not None:
126                    cursor = await db.execute(
127                        """SELECT * FROM messages
128                           ORDER BY created_at DESC
129                           LIMIT ?""",
130                        (limit,),
131                    )
...
141            for row in reversed(rows):  # Return in chronological order
```

**修复代码**:
```python
# 无需修改！实际代码已经是正确的：
# - SQL 查询使用 DESC 获取最新 N 条消息
# - reversed() 将其转换为时间顺序（最旧在上）
# 审核报告中的"ASC"描述有误
```

**状态**: ✅ **无需修改** - 代码逻辑正确

**影响范围**: 无

**风险**: 无

---

### 3. 添加上下文长度限制

**文件**: `E:\tools\ai-bot\niu_api\compat.py`

**修改位置**: 第 134 行

**当前代码**:
```python
134    history = await store.get_messages(limit=None)
135    logger.info(f"Loaded {len(history)} history messages")
```

**修复代码**:
```python
134    history = await store.get_messages(limit=50)  # 限制最近 50 条消息
135    logger.info(f"Loaded {len(history)} history messages")
```

**影响范围**:
- 影响所有对话请求的历史加载行为
- 100+ 轮对话将只加载最近 50 条消息

**风险**: 低
- 50 条消息约 10-15K tokens，在可控范围内
- 可通过日志验证实际加载情况
- 后续可实现动态 token 估算优化

**测试方法**:
- 创建 60+ 轮对话，验证只加载最近 50 条
- 监控 LLM API 调用的 token 使用量

---

### 4. 替换 assert 为类型检查

**文件**: `E:\tools\ai-bot\agent\generic\llmcore.py`

**修改位置**: 第 826 行

**当前代码**:
```python
826        assert type(msg) is dict
```

**修复代码**:
```python
826        if not isinstance(msg, dict):
827            raise TypeError(f"Expected dict, got {type(msg).__name__}: {msg}")
```

**影响范围**:
- 影响 `NativeClaudeSession.ask` 方法的输入验证
- 生产环境（Python -O 优化）下仍能捕获类型错误

**风险**: 低
- 更严格的类型检查
- 更清晰的错误信息

**测试方法**:
- 单元测试：传入非 dict 参数，验证抛出 TypeError
- 集成测试：正常对话流程不受影响

---

### 5. 初始化 content_blocks（最复杂的修复）

**文件**: `E:\tools\ai-bot\agent\generic\llmcore.py`

**修改位置**: 第 829-862 行

**当前代码**:
```python
829        content_blocks = None
830        gen = self.raw_ask(messages, tools, self.system, model)
831        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
...
```

**修复代码**:
```python
829        # 生成器模式：遍历生成器并获取返回值
830        gen = self.raw_ask(messages, tools, self.system, model)
831        content_blocks = []
832        try:
833            while True:
834                next(gen)  # 消费生成器的 yield 值（如果需要流式输出，应重构此方法）
835        except StopIteration as e:
836            # 从 StopIteration 异常中获取生成器的返回值
837            content_blocks = e.value or []
838
839        # 提取文本内容
840        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
```

**影响范围**:
- 修复 `NativeClaudeSession.ask` 方法的严重 bug
- 当前代码会导致 `TypeError: 'NoneType' object is not iterable`
- 影响所有使用 NativeClaudeSession 的 API 调用

**风险**: 中
- 核心代码路径，影响面大
- 需要完整测试 Claude API 调用流程
- 注意：此修复会消费生成器，如果调用者期望流式输出，需要进一步重构

**测试方法**:
- 单元测试：验证 `ask` 方法能正确处理生成器返回值
- 集成测试：完整对话流程测试
- 验证工具调用、thinking 等功能正常

**注意事项**:
- 审核报告中建议参考 BaseSession._ask_gen (656-673 行) 的模式
- 如果需要流式输出，应将 `ask` 改为生成器方法
- 当前修复方案会一次性消费所有生成器输出

---

### 6. 添加 JSON 解析异常处理

**文件**: `E:\tools\ai-bot\agent\generic\agent_loop.py`

**修改位置**: 第 120-127 行

**当前代码**:
```python
120            tool_calls = [
121                {
122                    "tool_name": tc.function.name,
123                    "args": json.loads(tc.function.arguments),
124                    "id": tc.id,
125                }
126                for tc in response.tool_calls
127            ]
```

**修复代码**:
```python
120            tool_calls = []
121            for tc in response.tool_calls:
122                try:
123                    args = json.loads(tc.function.arguments)
124                    tool_calls.append({
125                        "tool_name": tc.function.name,
126                        "args": args,
127                        "id": tc.id,
128                    })
129                except json.JSONDecodeError as e:
130                    logger.error(f"Failed to parse tool arguments for {tc.function.name}: {e}")
131                    logger.error(f"Raw arguments: {tc.function.arguments}")
132                    # 使用空参数继续执行，避免整个流程失败
133                    tool_calls.append({
134                        "tool_name": tc.function.name,
135                        "args": {},  # 回退为空参数
136                        "id": tc.id,
137                        "error": str(e),  # 记录错误信息
138                    })
```

**影响范围**:
- 影响工具调用参数解析逻辑
- LLM 返回非法 JSON 时不会崩溃

**风险**: 低
- 增强健壮性，避免单点故障
- 提供详细的错误日志便于调试

**测试方法**:
- 模拟 LLM 返回非法 JSON 格式的工具参数
- 验证错误被捕获并记录
- 验证后续流程继续执行

---

### 7. 使用 with 管理数据库连接

**文件**: `E:\tools\ai-bot\agent\handler.py`

**修改位置**: 第 679-688 行

**当前代码**:
```python
679                                conn = sqlite3.connect(db_path)
680                                cursor = conn.cursor()
681                                cursor.execute("""
682                                    SELECT id, content, status, scheduled_at
683                                    FROM scheduled_tasks
684                                    ORDER BY created_at DESC
685                                    LIMIT 1
686                                """)
687                                latest_task = cursor.fetchone()
688                                conn.close()
```

**修复代码**:
```python
679                                try:
680                                    with sqlite3.connect(db_path) as conn:
681                                        cursor = conn.cursor()
682                                        cursor.execute("""
683                                            SELECT id, content, status, scheduled_at
684                                            FROM scheduled_tasks
685                                            ORDER BY created_at DESC
686                                            LIMIT 1
687                                        """)
688                                        latest_task = cursor.fetchone()
689                                except sqlite3.Error as e:
690                                    yield f"[SubAgent] ⚠ Database error: {e}\n"
691                                    latest_task = None
```

**影响范围**:
- 影响 SubAgent 任务验证逻辑
- 确保数据库连接在异常时也能正确关闭

**风险**: 低
- 标准 Python 资源管理模式
- 增强异常情况下的资源管理

**测试方法**:
- 模拟数据库异常（如文件权限错误）
- 验证连接被正确关闭，无资源泄露
- 验证错误信息被正确记录

---

## 二、实施顺序

建议按以下顺序实施修复，避免依赖冲突并逐步验证：

### 第 1 步：修复 content_blocks 初始化（P0-5）
**原因**:
- 最严重的 bug，当前代码会导致运行时错误
- 修复后可以正常使用 NativeClaudeSession
- 为后续测试提供基础环境

**预计时间**: 30 分钟
**验证**: 运行基础对话测试

---

### 第 2 步：添加 JSON 解析异常处理（P0-6）
**原因**:
- 独立修复，不依赖其他修改
- 提升工具调用的健壮性
- 防止 LLM 返回异常数据时崩溃

**预计时间**: 15 分钟
**验证**: 模拟非法 JSON 输入测试

---

### 第 3 步：替换 assert 为类型检查（P0-4）
**原因**:
- 独立修复，简单直接
- 提升生产环境的稳定性
- 为后续测试提供更好的错误信息

**预计时间**: 10 分钟
**验证**: 类型错误测试

---

### 第 4 步：使用 with 管理数据库连接（P0-7）
**原因**:
- 独立修复，影响范围小
- 提升资源管理安全性
- 防止连接泄露

**预计时间**: 15 分钟
**验证**: 数据库异常测试

---

### 第 5 步：添加上下文长度限制（P0-3）
**原因**:
- 独立修复，影响范围明确
- 防止长对话导致的性能问题
- 需要在实际环境中测试 50 条限制的合理性

**预计时间**: 10 分钟
**验证**: 长对话测试（60+ 轮）

---

### 第 6 步：清理 BaseSession.history 废弃字段（P0-1）
**原因**:
- 影响最小，仅删除冗余代码
- 放在最后确保没有遗漏的依赖

**预计时间**: 5 分钟
**验证**: 运行完整测试套件

---

### 第 7 步：MessageStore 排序逻辑（P0-2）
**状态**: ✅ 无需修改 - 代码已经是正确的

---

## 三、依赖关系

**所有 P0 修复项相互独立，无依赖关系**

可以并行实施，但建议按上述顺序逐步验证：

```
P0-5 (content_blocks) ──┐
P0-6 (JSON 解析)    ────┤
P0-4 (assert 检查)  ────┤
P0-7 (数据库连接)   ────┼──> 集成测试
P0-3 (上下文限制)   ────┤
P0-1 (废弃字段)     ────┘
P0-2 (排序逻辑)     ────> 无需修改
```

---

## 四、验证方案

### 4.1 单元测试

```bash
# 测试 content_blocks 修复
pytest tests/test_llmcore.py::test_native_claude_session_ask -v

# 测试 JSON 解析异常处理
pytest tests/test_agent_loop.py::test_tool_call_json_parse -v

# 测试类型检查
pytest tests/test_llmcore.py::test_ask_type_validation -v

# 测试数据库连接管理
pytest tests/test_handler.py::test_database_connection -v

# 测试上下文限制
pytest tests/test_compat.py::test_history_limit -v
```

### 4.2 集成测试

**测试场景 1: 基础对话流程**
```bash
# 启动服务
python -m niu_api.main

# 发送测试请求
curl -X POST http://localhost:8000/api/chat/session \
  -H "Content-Type: application/json" \
  -d '{"session_id": "test", "message": "Hello"}'
```

**测试场景 2: 长对话历史限制**
```python
# 创建 60 轮对话
for i in range(60):
    response = client.post("/api/chat/session", json={
        "session_id": "long-conversation",
        "message": f"Message {i}"
    })

# 验证日志：应显示 "Loaded 50 history messages"
```

**测试场景 3: 工具调用异常处理**
```python
# 模拟 LLM 返回非法 JSON
# 验证错误被记录，流程继续执行
```

### 4.3 性能测试

```bash
# 监控内存占用
python -m memory_profiler niu_api.main

# 监控数据库连接
lsof -i | grep sqlite

# 压力测试
locust -f tests/load_test.py
```

### 4.4 验收标准

- [ ] 所有单元测试通过
- [ ] 基础对话功能正常
- [ ] 长对话加载限制在 50 条
- [ ] 工具调用非法 JSON 不崩溃
- [ ] 数据库连接无泄露
- [ ] 无 TypeError 或 assert 失败
- [ ] 日志输出正常，无异常堆栈

---

## 五、回滚方案

### 5.1 Git 版本控制

```bash
# 创建修复分支
git checkout -b fix/p0-critical-issues

# 每个修复单独提交
git add agent/generic/llmcore.py
git commit -m "fix: correct content_blocks initialization in NativeClaudeSession.ask"

# 如果需要回滚单个修复
git revert <commit-hash>

# 如果需要回滚所有修复
git checkout mac-compat-dev
git branch -D fix/p0-critical-issues
```

### 5.2 配置开关（可选）

对于上下文长度限制，可以添加配置开关：

```python
# niu_api/compat.py
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "50"))

history = await store.get_messages(limit=HISTORY_LIMIT)
```

### 5.3 降级策略

如果修复导致问题，可以快速降级：

**P0-5 (content_blocks)**:
- 如果流式输出受影响，可以恢复原代码并添加显式注释说明问题
- 优先级：核心功能 > 流式输出

**P0-3 (上下文限制)**:
- 如果 50 条限制导致对话质量下降，可临时调整为 100 条
- 通过环境变量快速调整：`HISTORY_LIMIT=100`

**P0-6 (JSON 解析)**:
- 如果空参数导致工具调用失败，可以改为抛出异常并终止流程

### 5.4 监控指标

部署后监控以下指标：

- 错误率：`TypeError`, `json.JSONDecodeError`, `sqlite3.Error`
- 性能：对话响应时间，内存占用
- 业务：对话轮数，历史消息加载数量
- 日志：异常堆栈出现频率

---

## 六、风险评估总结

| 修复项 | 风险级别 | 主要风险 | 缓解措施 |
|--------|----------|----------|----------|
| P0-1 废弃字段清理 | 低 | 无 | 直接删除，无依赖 |
| P0-2 排序逻辑 | 无 | 无 | 代码已正确，无需修改 |
| P0-3 上下文限制 | 低 | 对话质量下降 | 可配置，动态调整 |
| P0-4 assert 替换 | 低 | 无 | 标准最佳实践 |
| P0-5 content_blocks | 中 | 流式输出受影响 | 充分测试，必要时重构 |
| P0-6 JSON 解析 | 低 | 无 | 增强健壮性 |
| P0-7 数据库连接 | 低 | 无 | 标准资源管理 |

**总体风险**: 低-中

**建议**:
1. 先在测试环境完整验证
2. 生产环境灰度发布（10% 流量）
3. 监控 24 小时无异常后全量发布

---

### 关键文件列表（用于实施）

1. `E:\tools\ai-bot\agent\generic\llmcore.py` - P0-1, P0-4, P0-5
2. `E:\tools\ai-bot\agent\generic\agent_loop.py` - P0-6
3. `E:\tools\ai-bot\niu_api\compat.py` - P0-3
4. `E:\tools\ai-bot\agent\handler.py` - P0-7
5. `E:\tools\ai-bot\agent\session.py` - P0-2 (无需修改)
