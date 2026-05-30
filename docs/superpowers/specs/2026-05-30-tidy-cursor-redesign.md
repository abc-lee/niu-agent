# Auto-Tidy 管道双游标机制重构设计

## 问题

1. **Entity Extractor 无法报告游标**：通过 `history` 参数传递消息（无 idx/UUID 注解），子Agent不知道 UUID，只能返回 `null`，游标不推进，下次重复处理
2. **双写竞态**：handler.py 和 compat.py 都写游标文件，无互斥，可能互相覆盖
3. **主Agent误调用**：context-manager 和 entity-extractor 暴露给主Agent，主Agent调用时携带主Agent上下文，产生双写竞态
4. **Dream Evolver 传全量**：传入全量消息列表让子Agent自行过滤，浪费 token 且不可靠
5. **Context Manager 保护范围不可靠**：子Agent自行从文本判断"最近10条"，无程序兜底，时好时不好
6. **Dream Evolver 游标无 fallback**：与 Entity Extractor 修复前同样的问题
7. **WM 虚拟消息污染**：Entity Extractor 通过 history 传递时 `_build_entity_history()` 会过滤 WM 消息，但改为 task 方式后 `_build_incremental_msg_text()` 不过滤，WM 消息会出现在 task 文本中浪费 token

根因：三个子Agent的消息传递方式不统一，程序层面没有提供足够的结构化信息（游标、保护范围），把本应由程序保证的约束交给了 LLM 自行判断。

## 设计原则

1. **三个子Agent全部由程序自动调用，不暴露给主Agent**（dream-evolver 原本就不在主Agent列表中，只需移除 context-manager 和 entity-extractor）
2. **三个子Agent全部统一为 task 方式**（prompt 内嵌带注解的消息文本，不传 history）
3. **sleep 模式只传增量消息**（双游标范围内），force 模式 Entity Extractor 保持全量（溢出可能导致知识丢失需要重新提取），Dream Evolver 增量
4. **程序层面硬性保护**（最近 N 条不压缩），force 模式和 sleep 模式都有程序兜底
5. **游标单点写入**（只在 compat.py），消除双写竞态

## 架构

### Auto-Tidy 管道

```
sleep/force 触发
    ↓
_tidy_context_impl()
    ↓
1. 从 Message DB 读取当前会话全量消息
2. 从游标文件读取各子Agent的最后位置
3. 为每个子Agent计算增量消息范围（见下文"增量消息范围计算"）
4. 调用 _build_incremental_msg_text() 为每个子Agent生成带注解文本
   - 过滤 WM（working_memory）虚拟消息
   - 修复 tool_calls 成对完整性
   - Context Manager 的消息加 [PROTECTED] 标签
5. 依次调用子Agent（task 方式，不传 history）
6. 从子Agent输出提取新游标，写入文件
```

### 消息格式

`_build_incremental_msg_text()` 生成的文本格式：
```
[id:uuid1] [idx:3] 128tokens role:user: 消息内容...
[id:uuid2] [idx:4] 256tokens role:assistant: 消息内容...
```

- `id`: 消息在 DB 中的 UUID（持久标识，用于游标存储，跨会话不变）
- `idx`: 消息在全量消息列表中的序号（1-based，动态值，删除消息后会变）。不是增量列表中的相对序号 — 增量消息的 idx 与全量列表保持一致（如全量第51条，增量中 idx=51 而非 idx=1）。游标用 id（UUID）存储，idx 仅用于子Agent判断时间先后
- `tokens`: 消息的 token 数

### Context Manager 的保护范围

在 `msg_list_text` 中，对保护范围内的消息加 `[PROTECTED]` 标签：
```
[id:uuid1] [idx:3] 128tokens role:user: 消息内容...
[id:uuid2] [idx:4] 256tokens role:assistant: 消息内容...
[id:uuid3] [idx:5] 64tokens role:user: [PROTECTED] 消息内容...
[id:uuid4] [idx:6] 32tokens role:assistant: [PROTECTED] 消息内容...
```

- 保护数量从 `~/.niu/preferences.json` 的 `context.protectRecentCount` 读取，默认 10
- prompt 中说明带 `[PROTECTED]` 标签的消息不可删除/压缩
- **程序层面兜底**（force 模式）：执行压缩计划时，从 `fresh_messages` 中取最后 N 条的 ID，从 `valid_deletes` 和 `valid_updates` 中排除
- **程序层面兜底**（sleep 模式）：sleep 模式下子Agent通过 `session-manager` 工具直接操作 DB，程序无法拦截。改为在 prompt 中明确列出保护消息的 UUID 列表（如 `保护消息ID: [uuid3, uuid4, ...]`），并在 `_tidy_context_impl` 执行完 sleep 模式后校验：如果保护范围内的消息被删除，从 DB 恢复（记录警告日志）

### 增量消息范围计算

```
全量消息列表:  [m0, m1, m2, m3, m4, m5, m6, m7, m8, m9]
entity_cursor: m2
dream_cursor:  m4
compress_cursor: m1

Sleep 模式:
  Entity Extractor 范围: [m3, m4, m5, m6, m7, m8, m9]  (entity_cursor 之后到末尾)
  Dream Evolver 范围:    [m5, m6, m7, m8, m9]            (dream_cursor 之后到末尾)
  Context Manager 范围:  [m2, m3, m4]                      (compress_cursor 之后到 dream_cursor)

Force 模式:
  Entity Extractor 范围: [m0, m1, ..., m9]                (全量，溢出可能遗漏知识)
  Dream Evolver 范围:    [m5, m6, m7, m8, m9]            (增量，精加工不需要全量)
  Context Manager 范围:  [m0, m1, ..., m9]                (全量，压缩需要看全貌)
```

Context Manager 的范围是 `[compress_cursor, dream_cursor]`，不包含 dream 尚未处理的消息。程序保证只传入正确范围的消息，子Agent prompt 简化为"处理收到的全部消息"，不再需要子Agent自行用游标 idx 过滤范围。

### `_build_incremental_msg_text()` 改动

当前签名：
```python
def _build_incremental_msg_text(messages, last_cursor_id, out_msg_ids, msg_tokens=None)
```

新增参数：
```python
def _build_incremental_msg_text(
    messages,
    last_cursor_id,       # 下界游标 UUID（None 或空字符串 = 从头开始）
    out_msg_ids,          # 输出参数：增量消息的 UUID 列表
    msg_tokens=None,      # 每条消息的 token 数
    end_cursor_id=None,   # 上界游标 UUID（None = 到末尾），用于 Context Manager
    protect_recent=0,     # 保护最后 N 条消息（加 [PROTECTED] 标签）
    filter_wm=True,       # 过滤 WM（working_memory）虚拟消息
)
```

- `end_cursor_id`：Context Manager 的上界，只生成到该游标为止的消息
- `protect_recent`：对最后 N 条消息加 `[PROTECTED]` 标签
- `filter_wm`：过滤 WM 虚拟消息和修复 tool_calls 成对完整性（原 `_build_entity_history()` 的逻辑迁移至此）

## 改动清单

### 1. 主Agent工具列表清理

**文件**: `config/agents/niu.md`

sub agents 列表移除 `context-manager` 和 `entity-extractor`（dream-evolver 原本就不在列表中）：
```yaml
sub agents:
  - file-processor
  - event-manager
```

同时移除委托表中 `chat-with-context-manager` 和 `chat-with-entity-extractor` 的说明行。

### 2. Entity Extractor 改为 task 方式

**文件**: `niu_api/compat.py`

- 删除 `_build_entity_history()` 函数（其 WM 过滤和成对修复逻辑迁移到 `_build_incremental_msg_text()` 的 `filter_wm` 参数）
- Entity Extractor 调用改为 `call_subagent(history=None, task=msg_text + entity_prompt)`
- `msg_text` 由 `_build_incremental_msg_text()` 生成（entity_cursor 到末尾）
- 游标 fallback：子Agent返回 null 时，推进到增量消息最后一条的 UUID

**文件**: `config/agents/entity-extractor.md`

- prompt 改为 task 方式的消息格式描述
- 明确 `[id:UUID] [idx:N]` 格式（idx 是全量列表序号，不是增量相对序号）
- 明确游标报告方式：输出 `{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}`，即使无内容也必须推进游标

### 3. Dream Evolver 改为增量模式

**文件**: `niu_api/compat.py`

- Dream Evolver 调用改为只传 dream_cursor 之后的增量消息（与 Entity Extractor 同样的 `_build_incremental_msg_text()` 调用方式）
- 不再传全量消息列表让子Agent自行过滤
- 游标 fallback：与 Entity Extractor 一致

**文件**: `config/agents/dream-evolver.md`

- prompt 移除"在消息列表中找到游标 UUID 的 idx，只处理 idx 更大的消息"的自行过滤要求
- 程序已保证只传入增量消息，子Agent只需处理收到的全部消息
- prompt 明确告知"以下消息是 entity-extractor 新处理的，请对其中涉及的实体做精加工"

### 4. Context Manager 保护范围硬性保证

**文件**: `niu_api/compat.py`

- `_build_incremental_msg_text()` 增加 `end_cursor_id` 参数（上界截断，用于 Context Manager 的 `[compress_cursor, dream_cursor]` 范围）
- `_build_incremental_msg_text()` 增加 `protect_recent` 参数（对最后 N 条消息加 `[PROTECTED]` 标签）
- 保护数量从 `~/.niu/preferences.json` 读取
- force 模式执行压缩计划时，程序层面排除保护范围内的消息 ID
- sleep 模式执行完后校验保护范围内的消息是否被误删，如果被删则记录警告

**文件**: `config/agents/context-manager.md`

- prompt 增加 `[PROTECTED]` 标签说明
- 明确带标签的消息不可删除/压缩
- prompt 简化范围描述：程序已保证只传入正确范围的消息，子Agent只需处理收到的全部消息

### 5. 游标单点写入

**文件**: `agent/handler.py`

- 删除 entity-extractor 游标写入逻辑（`_call_subagent_gen` 中的游标提取和文件写入）
- 游标统一在 `compat.py` 的 `_tidy_context_impl` 中写入

### 6. Dream Evolver 游标 fallback

**文件**: `niu_api/compat.py`

- Dream Evolver 游标提取失败时，与 Entity Extractor 一致：推进到增量消息最后一条的 UUID

### 7. `_build_incremental_msg_text()` 增强

**文件**: `niu_api/compat.py`

- 新增 `end_cursor_id` 参数：上界游标，只生成到该游标为止的消息（用于 Context Manager）
- 新增 `protect_recent` 参数：对最后 N 条消息加 `[PROTECTED]` 标签
- 新增 `filter_wm` 参数：过滤 WM 虚拟消息和修复 tool_calls 成对完整性（原 `_build_entity_history()` 的逻辑迁移至此）
- idx 保持全量序号（与当前行为一致），不改为增量相对序号

## 游标文件格式

不变，保持现有格式：
```json
{
  "last_entity_extract_id": "uuid-xxx",
  "last_entity_extract_at": "2026-05-30T12:00:00"
}
```

每个子Agent有独立的游标文件：
- `~/.niu/last_entity_extract.json`
- `~/.niu/last_dream_evolve.json`
- `~/.niu/last_compress.json`

## 风险评估

| 改动 | 风险 | 缓解 |
|------|------|------|
| Entity Extractor 改 task 方式 | 中 | prompt 充分测试，确保 LLM 能正确解析新格式 |
| Dream Evolver 改增量 | 中 | 传入增量消息后 prompt 需要同步调整，明确告知"以下是新处理的消息" |
| Context Manager 保护范围 | 低 | force 模式程序硬性保证，sleep 模式事后校验 |
| 移除主Agent工具 | 低 | 三个子Agent本就不应由主Agent调用 |
| 删除 handler.py 游标写入 | 低 | compat.py 已有写入逻辑 |
| `_build_incremental_msg_text` 增强 | 低 | 新增参数都有默认值，不影响现有调用 |
| WM 过滤迁移 | 低 | 逻辑从 `_build_entity_history` 迁移到 `_build_incremental_msg_text`，行为不变 |
