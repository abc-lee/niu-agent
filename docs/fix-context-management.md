# 上下文管理优化 - 去除冗余删除机制

## 问题背景

用户发现 `/new` 清空对话后，LLM 仍然能访问到历史对话，导致上下文混乱。

## 根本原因

GenericAgent 有三层上下文管理：
1. **数据库（MessageStore）** - 持久化存储，用户可见
2. **BaseSession.history** - LLM 客户端内存缓存
3. **发送给 LLM 的 messages** - 每次请求构建

**关键问题**：`trim_messages_history` 会自动删除早期消息，导致：
- 数据库有 100 条消息
- BaseSession.history 只有 40 条（压缩后）
- 两者不一致，造成混乱

## 解决方案

### 核心原则

**数据库是唯一真实来源**：
- 数据库有多少条，就加载多少条
- 不人为删除早期消息
- BaseSession.history 只是临时状态，不做压缩

### 修改内容

#### 1. session.py - 支持无限制加载

```python
async def get_messages(self, limit: Optional[int] = None, ...):
    """
    Get messages (chronological order).
    If limit is None, return all messages.
    """
```

**改动**：
- `limit` 参数改为 `Optional[int]`
- `limit=None` 时，不加 `LIMIT` 子句
- 加载所有历史消息

#### 2. compat.py - 去掉 limit 限制

```python
# 修改前
history = await store.get_messages(limit=100)

# 修改后
history = await store.get_messages(limit=None)
```

**原因**：
- 上下文本就有上限（LLM 上下文窗口 128k/200k tokens）
- 几千条消息也就几兆，不应该人为限制
- 让数据库成为唯一真实来源

#### 3. llmcore.py - 去除删除逻辑

```python
def trim_messages_history(history, context_win):
    """Compress history tags without deleting messages."""
    compress_history_tags(history)
    cost = sum(len(json.dumps(m, ensure_ascii=False)) for m in history)
    print(f"[Debug] Current context: {cost} chars, {len(history)} messages.")
    # No longer delete early messages - keep all history
    # Let the database be the single source of truth
```

**改动**：
- 保留 `compress_history_tags`（压缩标签节省 token）
- **删除**后面的删除早期消息逻辑
- 不再破坏历史完整性

## 架构澄清

### BaseSession.history 的实际作用

根据深度分析发现：

**BaseSession.history 在 NiuRunner 中根本不会被调用！**

**调用链**：
```
compat.py → runner.py → agent_loop.py → ToolClient.chat
    ↓
    _build_protocol_prompt(messages)
    ↓
    self.backend.ask(full_prompt)  # 已经是字符串！
```

`ToolClient.chat` 已经把 messages 拼接成字符串，传给 `backend.ask()` 时不再维护 history。

**结论**：
- `trim_messages_history` 在 NiuRunner 中不会被调用
- 但修改它仍然有意义（兼容遗留调用链）
- 真正的限制在 `compat.py` 的 `limit=100`

### 两套历史的正确关系

| 数据库（MessageStore） | BaseSession.history |
|----------------------|---------------------|
| 持久化存储 | LLM 调用的临时状态 |
| 前端展示 | 多轮工具调用状态维护 |
| 用户可见 | 内部机制 |
| **不应该压缩** | **不应该压缩** |
| **唯一真实来源** | **数据库的镜像** |

## 影响评估

### ✅ 无冲突

- **handler.py 的硬编码限制**：操作 `self.history_info`（工作记忆），与 BaseSession.history 无关
- **agent_loop.py**：处理所有传入的 history，无限制
- **ToolClient**：构建完整 prompt，无限制

### ⚠️ 需要注意

- **LLM API 上下文窗口限制**：
  - GPT-4o: 128k tokens
  - Claude-3.5-Sonnet: 200k tokens
  - 几千条消息通常不会超限

- **性能优化**：
  - `compress_history_tags` 仍然有效，节省 token
  - 每 5 轮才压缩一次，性能开销低

## 测试验证

### 测试步骤

1. 重启服务：`Ctrl+C` → `go run main.go`
2. 发送几条消息
3. 输入 `/new` 清空
4. 发送新消息，验证是否清空成功
5. 刷新页面，验证历史是否正确

### 验证数据库

```bash
# 检查数据库消息数量
curl -s "http://127.0.0.1:9876/api/context/messages?limit=20" | python -c "import sys, json; data=json.load(sys.stdin); print(len(data['messages']), 'messages, total:', data['total_in_db'])"

# 清空数据库
curl -X POST http://127.0.0.1:9876/api/chat/clear
```

## 后续优化

### 长期方案

如果历史过长（超过 1000 条），可以实现智能压缩：

1. **外部压缩机制**：
   - 使用 context-manager 子 Agent 定期压缩
   - 保留最近 N 条完整消息
   - 早期消息生成摘要

2. **分层存储**：
   - L0：最近 10 条完整消息
   - L1：摘要 + 指针
   - L2：向量库检索

3. **动态加载**：
   - 根据 prompt 长度动态调整加载数量
   - 优先加载最近的消息

## 相关文档

- `docs/analysis-context-management-issue.md` - 上下文管理问题分析
- `docs/analysis-context-redundancy.md` - 冗余层分析
- `docs/analysis-basesession-history.md` - BaseSession.history 深度分析
- `docs/analysis-trim-history-issue.md` - trim_messages_history 问题分析
- `docs/analysis-disable-trim-feasibility.md` - 禁用压缩可行性分析

## 提交信息

```
fix: 去除上下文管理冗余删除机制

- session.py: get_messages 支持 limit=None，加载所有历史
- compat.py: 去掉 limit=100 限制
- llmcore.py: trim_messages_history 只压缩标签不删除消息

数据库成为唯一真实来源，BaseSession.history 不再压缩。
```
