# Context Manager 工具设计文档

> 创建日期：2026-03-29
> 状态：设计阶段

## 1. 概述

Context Manager 需要配套工具才能实现智能压缩。本文档定义所需工具的接口和实现方案。

## 2. 三种触发机制

| 触发 | 紧迫性 | 行为 |
|------|--------|------|
| 闲置触发 | 低 | 适度压缩，像文档整理 |
| 接近上限 | 高 | 强制压缩 |
| 大模型报错 | 高 | 强制压缩 |

**触发条件**：
```
available_window = context_window_size - input_token
```

当当前使用量接近 `available_window` 时触发。

## 3. 工具列表

### 3.1 calculate_token_usage

**功能**：返回当前 Token 使用率和各层级大小

**输入**：无

**输出**：
```json
{
  "total_tokens": 150000,
  "available_window": 160000,
  "usage_rate": 0.94,
  "need_compression": true,
  "layers": {
    "L0": {
      "count": 5,
      "tokens": 20000,
      "description": "对话核心摘要，近期优先保留"
    },
    "L1": {
      "count": 15,
      "tokens": 30000,
      "description": "极简格式摘要，向量检索为主（详见 spec-L1-summary.md）"
    },
    "L2": {
      "count": 80,
      "tokens": 100000,
      "description": "完整内容，已入向量库可删除"
    }
  }
}
```

**实现位置**：main.go

**实现逻辑**：
1. 遍历历史消息，统计 Token
2. 根据消息类型区分 L0/L1/L2
3. 计算使用率，判断是否需要压缩

---

### 3.2 get_messages_by_layer

**功能**：按层级返回消息列表

**输入**：
```json
{
  "layer": "L2",        // L0/L1/L2
  "order": "oldest_first",  // oldest_first 或 newest_first
  "limit": 20           // 可选，限制返回数量
}
```

**输出**：
```json
{
  "messages": [
    {
      "id": "msg_001",
      "role": "user",
      "content": "明天下午3点开会",
      "timestamp": "2026-03-29T10:00:00Z",
      "tokens": 15
    },
    ...
  ],
  "total_count": 80,
  "total_tokens": 100000
}
```

**实现位置**：main.go 或 session 包

**实现逻辑**：
1. 从数据库查询消息
2. 根据时间/类型判断层级
3. 按指定顺序返回

---

### 3.3 delete_messages

**功能**：删除指定 ID 的消息

**输入**：
```json
{
  "message_ids": ["msg_001", "msg_002", "msg_003"],
  "reason": "compression - 远期 L2 清理"
}
```

**输出**：
```json
{
  "deleted_count": 3,
  "deleted_tokens": 500,
  "new_total_tokens": 149500
}
```

**实现位置**：main.go 或 session 包

**实现逻辑**：
1. 从数据库删除指定消息
2. 更新 Token 统计
3. 记录删除原因

---

### 3.4 delete_document（向量库删除）

**功能**：从向量库删除指定文档/记忆

**输入**：
```json
{
  "document_id": "doc_001",  // 可选，按 ID 删除
  "query": "开会",           // 可选，按内容搜索删除
  "filter": {               // 可选，按 metadata 过滤删除
    "type": "event",
    "status": "cancelled"
  }
}
```

**输出**：
```json
{
  "deleted_count": 3,
  "deleted_ids": ["doc_001", "doc_002", "doc_003"]
}
```

**实现位置**：vector-store MCP server

**实现逻辑**：
1. 支持按 ID、内容、metadata 删除
2. 删除向量索引
3. 返回删除结果

---

### 3.5 compress_history（压缩执行）

**功能**：执行压缩操作，返回压缩后的消息列表

**输入**：
```json
{
  "mode": "idle",  // idle（适度）、aggressive（强制）
  "target_tokens": 100000,  // 目标 Token 数
  "strategy": "remove_oldest_L2_first"  // 压缩策略
}
```

**输出**：
```json
{
  "original_tokens": 150000,
  "new_tokens": 95000,
  "removed_messages": 25,
  "strategy_used": "remove_oldest_L2_first"
}
```

**实现位置**：main.go

**实现逻辑**：
1. 调用 calculate_token_usage 获取当前状态
2. 根据策略选择要删除的消息
3. 调用 delete_messages 执行删除
4. 返回压缩结果

## 4. 压缩策略

### 4.1 适度压缩（idle 模式）

```
1. 检查 L2 是否有重复内容
2. 合并相似消息
3. 删除简单的确认回复
4. 保留最近 30 条消息
```

### 4.2 强制压缩（aggressive 模式）

```
第1步：L2 按比例硬删（50%），不经过 Agent
第2步：如果还不够，把 L1 列表给 Agent，让它判断删哪些
第3步：如果还不够，删末段 L0（最后手段）
```

**分工原因**：
- L2（长期记忆）：数量大，按比例删效率高，不需要智能判断
- L1（工作记忆）：需要智能判断哪些重要，由 Agent 决定
- L0（核心记忆）：最后才删，只删末段（最老的）

## 5. 实现状态

| 优先级 | 工具 | 说明 |
|--------|------|------|
| P0 | delete_document | 向量库删除，用户现在就需要 |
| P0 | calculate_token_usage | Token 统计，压缩前置条件 |
| P1 | get_messages_by_layer | 按层级获取消息 |
| P1 | delete_messages | 删除消息 |
| P1 | compress_history | 压缩执行 |

## 6. 实现状态

### 已完成

| 工具 | 状态 | 说明 |
|------|------|------|
| delete_document | ✅ 完成 | vector-store 支持 ID/query/filter 删除 |
| calculate_token_usage | ✅ 完成 | GET /api/context/usage |
| get_messages_by_layer | ✅ 完成 | GET /api/context/messages |
| delete_messages | ✅ 完成 | POST /api/context/messages/delete |

### 待实现

| 工具 | 状态 | 说明 |
|------|------|------|
| compress_history | 待实现 | 压缩执行，调用其他工具组合完成 |

## 7. 下一步

1. 更新 Context Manager 提示词，指导 Agent 使用这些工具
2. 实现触发机制（闲置/超上限/大模型报错）
3. 测试压缩流程
