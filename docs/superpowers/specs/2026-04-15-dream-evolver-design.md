# 梦境进化子Agent设计

> 日期：2026-04-15
> 关联：KG数据流入渠道3（聊天→KG）+ 渠道4（便利贴→KG）

## 问题

当前 context-manager 在 sleep 时承担了过多职责：压缩 + 学习 + 用户建模。其中学习/建模工作需要完整对话内容，但 context-manager 做完这些后才开始压缩，顺序不合理。更关键的是，对话中的实体/关系从未写入知识图谱（KG渠道3/4空缺）。

## 方案

将 context-manager 的学习/建模职责拆分给独立的"梦境进化"子Agent，context-manager 只保留压缩职责。

**执行顺序**：sleep → 梦境进化（增量学习+KG写入）→ 内容管理（压缩删除）

## 架构

### 触发机制

**当前**：sleep → `triggerTidy()` → POST `/api/context/tidy` → context-manager 子Agent

**改为**：sleep → `triggerTidy()` → POST `/api/context/tidy` → **dream-evolver 子Agent** → **context-manager 子Agent**

- `niu_api/compat.py` 的 `/api/context/tidy` 端点中，`mode == "sleep"` 时先调用 `call_subagent("dream-evolver", ...)`，等待完成后再调用 `call_subagent("context-manager", ...)`
- 两次调用串行执行，确保梦境进化先读完对话再让压缩删除

### 增量游标

文件 `~/.niu/last_dream_evolve.json`：
```json
{
  "last_message_id": 42,
  "last_evolve_at": "2026-04-15T21:00:00",
  "stats": { "entities_created": 5, "experiences_extracted": 3 }
}
```

- 梦境进化启动时读取 `last_message_id`，只处理 ID > 游标的新消息
- 处理完成后更新游标为本次处理的最大 message ID
- 即使消息被后续压缩删除，游标仍有效（只向前推进）
- 首次运行（无游标文件）处理全部消息

### 梦境进化工作项

| # | 工作项 | 输入 | 输出 | 存储 |
|---|--------|------|------|------|
| 1 | 错误经验提取 | 用户纠正Agent的消息 | 错误模式+正确做法的l1 | 向量库 |
| 2 | 成功经验提取 | 任务成功完成的消息 | 成功经验的l1 | 向量库 |
| 3 | 工具方言学习 | 用户表达→工具调用映射 | tool_dialect l1 | 向量库 |
| 4 | 用户状态推断 | 语气词、情绪信号 | user_state l1 | 向量库 |
| 5 | 用户画像深化 | 事实/偏好/习惯/性格 | user_profile l1 | 向量库 |
| 6 | KG实体/关系写入 | 对话中实体和关系 | Entity节点+MENTIONS边 | KuzuDB |

**注意**：Skill进化不在梦境进化范围内 — 主Agent在执行复杂任务时按 `docs/spec-skills.md` 规范实时编写Skill，这是"当事人"视角的工作。

### 子Agent定义

**文件**：`config/agents/dream-evolver.md`

```yaml
---
name: dream-evolver
description: 梦境进化 - 睡眠时从对话中提取知识、学习经验、写入知识图谱
mode: subagent
temperature: 0.3
mcpServers:
  - vector-store
  - kg-server
  - session-manager
---
```

**权限说明**：
- `vector-store`：读写经验、画像、方言
- `kg-server`：写入实体/关系（梦境进化是唯一被授权使用 kg-server 写入工具的子Agent）
- `session-manager`：读取对话历史

### context-manager 精简

从 `config/agents/context-manager.md` 中移除：
- 第5步：错误经验提取
- 第6步：成功经验提取
- 工具方言提取章节
- 用户状态推断章节
- 用户画像提取章节

保留：
- l0/l1/l2 压缩逻辑
- 会话单元识别
- 消息删除规则
- 强制压缩模式
- 记忆去重（压缩时合并重复的l1）

### API层改动

**文件**：`niu_api/compat.py`，`/api/context/tidy` 端点

当前逻辑（`mode == "sleep"`）：
```python
result = await call_subagent(agent_name="context-manager", task=prompt, ...)
```

改为：
```python
# 1. 先调梦境进化（增量学习+KG写入）
dream_prompt = _build_dream_evolver_prompt(session_id, messages)
dream_result = await call_subagent(agent_name="dream-evolver", task=dream_prompt, ...)

# 2. 再调内容管理（压缩删除）
compress_prompt = _build_context_manager_prompt(session_id, messages, mode)
compress_result = await call_subagent(agent_name="context-manager", task=compress_prompt, ...)
```

### KG写入规则（工作项6）

梦境进化从对话中提取实体和关系，写入KuzuDB：

1. **实体提取**：从用户/AI消息中识别命名实体（人名、组织、技术、地点等）
2. **Document节点**：每段有意义的对话创建一个Document节点（source="chat"）
3. **Entity节点**：每个识别的实体创建Entity节点（type由上下文推断）
4. **MENTIONS边**：Document MENTIONS Entity，confidence=0.5（Agent推断级别）
5. **RELATED_TO边**：同一对话中共同出现的实体之间建立关系，confidence=0.3

**置信度**：
- 对话中明确提及的实体：0.5
- 推断的关系：0.3
- 用户手动确认：1.0（暂不实现，留给未来）

### 模型选择

使用当前配置的模型（与主Agent相同）。梦境进化是后台任务，不追求实时响应，但需要足够的推理能力来提取实体/关系/经验。

## 文件变更

| 文件 | 操作 | 说明 |
|------|------|------|
| `config/agents/dream-evolver.md` | 新建 | 梦境进化子Agent定义+提示词 |
| `config/agents/context-manager.md` | 修改 | 移除学习/建模章节，只保留压缩 |
| `niu_api/compat.py` | 修改 | `/api/context/tidy` 先调dream-evolver再调context-manager |
| `~/.niu/last_dream_evolve.json` | 运行时生成 | 增量游标文件 |

## 验证

1. sleep触发后，日志显示 dream-evolver 先执行，context-manager 后执行
2. 梦境进化处理新消息后，游标文件更新
3. 对话中提及的实体出现在KG图谱中
4. 错误/成功经验出现在向量库中（category=interaction_habit）
5. context-manager 不再执行学习/建模工作
6. 压缩后消息减少，但KG和向量库数据保留
