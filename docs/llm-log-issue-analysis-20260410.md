# LLM 交互日志问题分析报告

> 分析时间：2026-04-10
> 发现者：用户观察
> 问题：日志中缺少历史聊天记录

---

## 🔍 问题发现

### 用户观察

从日志文件 `llm_interaction_20260410.log` 发现：
- ✅ 每次交互都记录了系统提示词
- ✅ 每次交互都记录了当前用户输入
- ❌ **缺少历史对话记录**
- ❌ **无法看到完整的上下文**

### 日志示例

```
[2026-04-10 12:16:22] MiniMax-M2.7-highspeed
[系统提示词]
# Role: 妞妞...

[用户输入]
用户拖入了以下文件...

[可用工具]
  - code_run
  - ...
```

**缺失内容**：
- ❌ 之前的用户消息
- ❌ 之前的AI回复
- ❌ 工具调用历史
- ❌ 完整的对话上下文

---

## 🎯 根因分析

### 日志记录位置

**文件**：`agent/generic/litellm_adapter.py`
**函数**：`_format_request_log()`
**行号**：56-100

### 问题代码

```python
def _format_request_log(f, log_entry: Dict[str, Any]):
    """格式化请求日志"""
    messages = log_entry.get("messages", [])
    
    # 第69-76行：只取第一条 system 消息
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            # 截断太长内容
            if len(content) > 600:
                content = content[:600] + "\n...（已截断）"
            f.write(f"[系统提示词]\n{content}\n\n")
            break
    
    # 第78-85行：只取最后一条 user 消息
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if len(content) > 400:
                content = content[:400] + "\n...（已截断）"
            f.write(f"[用户输入]\n{content}\n\n")
            break
```

### 问题根源

**设计缺陷**：
- ✅ `_write_interaction_log()` 接收了完整的 `messages` 数组（第253行）
- ❌ `_format_request_log()` 只提取了第一条 system 和最后一条 user
- ❌ **忽略了所有历史消息**
- ❌ **忽略了 assistant 消息**
- ❌ **忽略了 tool 消息**

---

## 📊 调用链分析

### 完整调用链

```
NiuRunner.chat()
  ↓ 传入 history 参数
agent_runner_loop()
  ↓ 构建 messages 数组（包含历史）
ToolClient.chat()
  ↓ 传入完整 messages
LiteLLMAdapter.chat()
  ↓ 调用 litellm.completion()
_write_interaction_log()
  ↓ 记录完整 messages
_format_request_log()
  ↓ ❌ 只提取部分消息
日志文件
```

### GitNexus 依赖链

```
_write_interaction_log (litellm_adapter.py:20)
  ↓ 调用
_format_request_log (litellm_adapter.py:56)
  ↓ 被 chat() 调用
LiteLLMAdapter.chat (litellm_adapter.py:215)
  ↓ 被 ToolClient.chat 调用
agent_runner_loop (agent_loop.py:70)
  ↓ 被 NiuRunner.chat 调用
NiuRunner.chat (runner.py:343)
```

---

## 🚨 影响评估

### 问题影响

| 影响项 | 严重程度 | 说明 |
|--------|----------|------|
| 调试能力 | HIGH | 无法追溯完整对话流程 |
| 问题复现 | HIGH | 缺少上下文导致难以复现问题 |
| 日志完整性 | MEDIUM | 日志不完整，信息丢失 |
| 开发效率 | MEDIUM | 增加调试时间 |

### 现有日志的价值

**✅ 保留了**：
- 系统提示词（确认注入内容）
- 当前用户输入（确认触发内容）
- 可用工具列表（确认工具注入）
- AI回复（确认输出内容）
- 工具调用（确认工具选择）
- 思考链（确认推理过程）

**❌ 缺失了**：
- 历史用户消息
- 历史AI回复
- 历史工具调用
- 完整对话上下文

---

## 💡 解决方案

### 方案1：完整记录历史（推荐）

**修改 `_format_request_log()` 函数**：

```python
def _format_request_log(f, log_entry: Dict[str, Any]):
    """格式化请求日志"""
    ts = log_entry.get("timestamp", "")
    model = log_entry.get("model", "")
    messages = log_entry.get("messages", [])
    tools = log_entry.get("tools", [])

    # 分隔线
    f.write(f"\n{'=' * 60}\n")
    f.write(f"[{ts}] {model}\n")
    f.write(f"{'=' * 60}\n")

    # 系统提示词
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if len(content) > 600:
                content = content[:600] + "\n...（已截断）"
            f.write(f"[系统提示词]\n{content}\n\n")
            break

    # ✅ 新增：历史对话记录
    history_msgs = [m for m in messages if m.get("role") in ("user", "assistant", "tool")]
    if len(history_msgs) > 1:  # 有历史消息
        f.write(f"[历史对话]（共{len(history_msgs)}条消息）\n")
        for i, msg in enumerate(history_msgs[:-1], 1):  # 排除最后一条（当前输入）
            role = msg.get("role", "")
            content = msg.get("content", "")
            if len(content) > 200:
                content = content[:200] + "..."
            f.write(f"{i}. [{role}] {content}\n")
        f.write("\n")

    # 当前用户输入
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if len(content) > 400:
                content = content[:400] + "\n...（已截断）"
            f.write(f"[用户输入]\n{content}\n\n")
            break

    # ... 后续代码不变
```

**优点**：
- ✅ 保留完整历史
- ✅ 便于调试和复现
- ✅ 日志更完整

**缺点**：
- ⚠️ 日志文件会变大
- ⚠️ 需要控制历史长度

---

### 方案2：可选记录历史

**添加参数控制**：

```python
def _write_interaction_log(log_entry: Dict[str, Any], include_history: bool = False):
    """写入 LLM 交互日志"""
    # ...
    if log_entry["type"] == "request":
        _format_request_log(f, log_entry, include_history)
```

**优点**：
- ✅ 灵活控制
- ✅ 可按需开启

**缺点**：
- ⚠️ 需要配置管理

---

### 方案3：单独的历史日志

**创建独立的历史日志文件**：

```python
def _write_history_log(messages: List[Dict]):
    """单独记录历史消息"""
    log_file = log_dir / f"chat_history_{datetime.now().strftime('%Y%m%d')}.log"
    # ...
```

**优点**：
- ✅ 主日志简洁
- ✅ 历史日志完整

**缺点**：
- ⚠️ 需要查看两个文件

---

## 📋 建议

### 立即行动（推荐方案1）

1. **修改 `_format_request_log()` 函数**
   - 添加历史对话记录部分
   - 限制历史长度（如最近5轮对话）
   - 控制单条消息长度（如200字符）

2. **验证修改**
   - 运行测试确保不影响现有功能
   - 检查日志文件大小增长

3. **文档更新**
   - 更新日志格式说明
   - 记录修改原因和影响

### 中期优化

1. **日志轮转**
   - 按天或大小自动轮转
   - 压缩历史日志

2. **日志分析工具**
   - 开发日志查看工具
   - 支持搜索和过滤

---

## 🎯 影响范围

### 需要修改的文件

| 文件 | 修改内容 | 风险 |
|------|----------|------|
| `agent/generic/litellm_adapter.py` | 修改 `_format_request_log()` | LOW |

### GitNexus 影响分析

**修改函数**：`_format_request_log`
**调用者**：`_write_interaction_log`
**影响范围**：所有 LLM 交互日志
**风险等级**：LOW（仅日志格式变更，不影响业务逻辑）

---

## 📝 总结

### 问题

- ❌ 日志中缺少历史对话记录
- ❌ `_format_request_log()` 只提取部分消息
- ❌ 影响调试和问题复现

### 解决方案

- ✅ 推荐方案1：修改 `_format_request_log()` 添加历史记录
- ✅ 控制历史长度避免日志过大
- ✅ LOW 风险，易于实现

### 后续

1. 实施方案1
2. 测试验证
3. 监控日志大小
4. 必要时优化存储策略

---

**分析完成时间**：2026-04-10
**问题严重程度**：MEDIUM
**建议优先级**：HIGH
