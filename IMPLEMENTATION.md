# GenericAgent 整合实施报告

## 背景

GenericAgent 与我们的系统存在双轨冲突：
- **GenericAgent**: `BaseSession.history` (内存) + `trim_messages_history()` 压缩
- **我们的系统**: `MessageStore` (SQLite) + `VectorSearchAdapter` 知识注入

**核心原则**: 不直接使用 GenericAgent 代码，只借鉴其概念，用我们的架构重新实现

**目标**:
- GenericAgent 有的能力 → 必须有（用我们的方式实现）
- GenericAgent 没有的能力 → 也要有（比它更强）

---

## 实施的修改

### 1. 解耦 GenericAgent 历史管理

**问题**: `BaseSession` 和 `NativeClaudeSession` 独立管理 `self.history`，与 `MessageStore` 冲突

**修改文件**: `agent/generic/llmcore.py`

**修改内容**:
- `BaseSession.ask()`: 移除 `self.history.append()` 和 `trim_messages_history()` 调用
- `NativeClaudeSession.ask()`: 同样移除内部历史管理
- `NativeToolClient`: 更新注释说明不再管理历史

**结果**: Session 层不再维护独立历史，完全由外部（`NiuRunner`）管理

---

### 2. 修复 agent_runner_loop 消息累积

**问题**: 第 178 行 `messages = [...]` 是 reassign 而不是 append，导致多轮对话时历史丢失

**修改文件**: `agent/generic/agent_loop.py`

**修改内容**:
```python
# 修改前
messages = [{"role": "user", "content": next_prompt, "tool_results": tool_results}]

# 修改后
messages.append({"role": "user", "content": next_prompt, "tool_results": tool_results})
```

**结果**: 多轮对话时消息正确累积

---

### 3. 新增经验总结能力（超越 GenericAgent）

**概念借鉴**: GenericAgent 的 `do_start_long_term_update` 工具

**新建文件**: `agent/experience_summarizer.py`

**核心功能**:
- 追踪工具执行结果（成功/失败）
- 判断是否值得总结（任务成功/高轮数/多次工具调用）
- 自动生成 Skill 文件
- 写入 `memory/skills/` 目录

**触发条件**（至少满足 2 个）:
- 任务成功完成
- 超过 10 轮对话
- 至少 3 次工具调用
- 关键工具执行成功

**优势对比**:

| 能力 | GenericAgent | 我们的实现 |
|------|-------------|-----------|
| 触发方式 | 手动调用工具 | 自动 + 手动 |
| 存储方式 | 自己的 memory/ 目录 | 统一 `memory/skills/` |
| 检索方式 | 简单文件搜索 | `VectorSearchAdapter` 语义检索 |
| 同步方式 | 无自动同步 | `injector/sync.py` 自动扫描 |

---

### 4. 新增自主探索能力（GenericAgent 没有的）

**概念借鉴**: GenericAgent 的 `autonomous_operation_sop`

**新建文件**: `agent/autonomous_explorer.py`

**核心功能**:
- 监控用户空闲时间
- 空闲 30 分钟触发反思模式
- 盘点 Skills 数量、记忆统计
- 生成改进建议

**优势**:
- 复用现有的 scheduler 定时任务系统
- Skills 自动被向量库索引
- 可选的回调函数用于自定义行为

---

### 5. 集成到 NiuHandler

**修改文件**: `agent/handler.py`

**新增内容**:
- 导入 `ExperienceSummarizer`
- 初始化 `_experience_context` 和 `_experience_summarizer`
- 新增 `_track_tool_execution()` 追踪工具执行
- 新增 `_check_and_summarize_experience()` 检查并总结经验

**结果**: Agent 在执行任务时会自动追踪并总结经验

---

## 新增文件清单

| 文件 | 说明 |
|------|------|
| `agent/experience_summarizer.py` | 经验总结器 |
| `agent/autonomous_explorer.py` | 自主探索器 |
| `memory/skills/` | Skills 存储目录 |
| `IMPLEMENTATION.md` | 本文档 |

---

## 修改文件清单

| 文件 | 修改内容 |
|------|---------|
| `agent/generic/llmcore.py` | 移除 Session 内部历史管理 |
| `agent/generic/agent_loop.py` | 修复消息累积逻辑 |
| `agent/handler.py` | 集成经验总结追踪 |

---

## 验证方案

1. **基础功能**: `./niu_test` 正常启动，64 个 MCP 工具加载

2. **消息历史**: 多轮对话后，`~/.niu/messages.db` 有正确记录

3. **Skills 注入**: 添加 `memory/skills/test_skill.md`，60 秒内被索引，对话中被注入

4. **经验总结**: Agent 执行多轮对话（>10轮），检查 `memory/skills/` 有新文件生成

5. **工具循环**: Agent 调用工具链正确执行（如 `remember` → `recall`）

---

## 架构对比

### 修改前

```
┌─────────────────────────────────────────────────────────────┐
│ agent/generic/llmcore.py (BaseSession)                     │
│   - self.history = []  内存列表                             │
│   - trim_messages_history() 管理上下文长度                  │
│   ❌ 与 MessageStore 冲突                                    │
└─────────────────────────────────────────────────────────────┘
```

### 修改后

```
┌─────────────────────────────────────────────────────────────┐
│ NiuRunner (agent/runner.py)                                │
│   - 从 MessageStore 加载历史                                │
│   - 组装 messages 列表                                      │
│   - 调用 agent_runner_loop                                  │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ agent_runner_loop (agent/generic/agent_loop.py)              │
│   - 接收预组装好的 messages                                 │
│   - 工具循环执行                                            │
│   - 消息累积（append 而非 reassign）                        │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│ BaseSession / NativeClaudeSession (agent/generic/llmcore.py)│
│   - 只做 LLM 调用，不管理历史                                │
│   ✅ 历史完全由外部管理                                      │
└─────────────────────────────────────────────────────────────┘
```
