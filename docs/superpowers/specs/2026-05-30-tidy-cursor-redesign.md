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

**文件**: `agent/handler.py`

- 删除 `_call_subagent_gen` 中 entity-extractor 的 history 特殊分支（第864-884行：`if agent_name == "entity-extractor"` 的 WM 过滤和 history 构建）
- 删除 `_call_subagent_gen` 中 entity-extractor 的游标写入逻辑（第932-956行）
- 在 `dispatch()` 的 `chat-with-*` 路由中增加屏蔽：对 `context-manager` 和 `entity-extractor` 返回"此子Agent已由系统自动管理，不可手动调用"

### 2. Entity Extractor 改为 task 方式

**文件**: `niu_api/compat.py`

- 删除 `_build_entity_history()` 函数（其 WM 过滤和成对修复逻辑迁移到 `_build_incremental_msg_text()` 的 `filter_wm` 参数）
- **sleep 模式**（第893-902行）：将 `call_subagent(history=incremental_entity_history, task=entity_prompt)` 改为 `call_subagent(history=None, task=entity_msg_text + entity_prompt)`，其中 `entity_msg_text` 由 `_build_incremental_msg_text(messages, last_entity_extract_id, entity_msg_ids, msg_tokens, filter_wm=True)` 生成
- **force 模式**（第1142行）：将 `history=_build_entity_history(messages, "")` 改为 `history=None`，task 改为增量消息文本（force 模式 entity_cursor 传空字符串 = 全量）
- 游标 fallback：子Agent返回 null 时，推进到增量消息最后一条的 UUID
- 空增量范围跳过：已有 `if entity_msg_ids:` 检查（第889行），保持不变

**文件**: `config/agents/entity-extractor.md`

- prompt 改为 task 方式的消息格式描述
- 明确 `[id:UUID] [idx:N]` 格式（idx 是全量列表序号，不是增量相对序号）
- 明确游标报告方式：输出 `{"last_entity_extract_id": "<收到的消息中 idx 最大的消息的 id（UUID）>"}`，即使无内容也必须推进游标

### 3. Dream Evolver 改为增量模式

**文件**: `niu_api/compat.py`

- **sleep 模式**（第953-988行）：将全量 `msg_list_text` 替换为 `_build_incremental_msg_text(messages, last_dream_evolve_id, dream_msg_ids, msg_tokens, filter_wm=True)` 生成的增量消息文本。prompt 简化为"对以下消息中涉及的实体做精加工"，移除"只处理游标之后的消息"的自行过滤要求
- **force 模式**（第1174-1194行）：同样改为增量消息文本
- 游标 fallback：与 Entity Extractor 一致
- 空增量范围跳过：新增 `if dream_msg_ids:` 检查，无增量消息时跳过调用

**文件**: `config/agents/dream-evolver.md`

- prompt 移除"在消息列表中找到游标 UUID 的 idx，只处理 idx 更大的消息"的自行过滤要求
- 程序已保证只传入增量消息，子Agent只需处理收到的全部消息
- prompt 简化为"对以下消息中涉及的实体做精加工"（不提及 Entity Extractor，两者独立）

### 4. Context Manager 保护范围硬性保证

**文件**: `niu_api/compat.py`

- `_build_incremental_msg_text()` 增加 `end_cursor_id` 参数（上界截断，用于 Context Manager 的 `[compress_cursor, dream_cursor_new]` 范围）
- `_build_incremental_msg_text()` 增加 `protect_recent` 参数（对最后 N 条消息加 `[PROTECTED]` 标签）
- 保护数量从 `~/.niu/preferences.json` 的 `context.protectRecentCount` 读取，默认 10
- **sleep 模式**（第1027-1048行）：将全量 `msg_list_text` 替换为 `_build_incremental_msg_text(messages, last_compress_id, compress_msg_ids, msg_tokens, end_cursor_id=new_dream_id, protect_recent=N, filter_wm=True)` 生成的增量范围消息文本。prompt 简化为"处理收到的全部消息"
- force 模式执行压缩计划时，程序层面排除保护范围内的消息 ID
- sleep 模式执行完后校验保护范围内的消息是否被误删，如果被删则记录警告
- 空增量范围跳过：新增 `if compress_msg_ids:` 检查，无增量消息时跳过调用

**文件**: `config/agents/context-manager.md`

- prompt 增加 `[PROTECTED]` 标签说明
- 明确带标签的消息不可删除/压缩
- prompt 简化范围描述：程序已保证只传入正确范围的消息，子Agent只需处理收到的全部消息

### 5. 串行调用消息刷新

**文件**: `niu_api/compat.py`

- 每个子Agent执行前重新获取消息列表：`messages = await store.get_messages()`
- 重新计算 `msg_tokens` 和 `msg_id_set`
- 记录 `dream_cursor_new`（Dream 推进后的游标，含 fallback），作为 Context Manager 的上界

### 6. Dream Evolver 游标 fallback

**文件**: `niu_api/compat.py`

- Dream Evolver 游标提取失败时，与 Entity Extractor 一致：推进到增量消息最后一条的 UUID
- `_extract_cursor_id()` 增加对 `null` 值的检测，返回特殊标记区分"没报告游标"和"明确返回 null"

### 7. `_build_incremental_msg_text()` 增强

**文件**: `niu_api/compat.py`

- 新增 `end_cursor_id` 参数：上界游标，只生成到该游标为止的消息（用于 Context Manager）
- 新增 `protect_recent` 参数：对最后 N 条消息加 `[PROTECTED]` 标签
- 新增 `filter_wm` 参数：过滤 WM 虚拟消息和修复 tool_calls 成对完整性（原 `_build_entity_history()` 的逻辑迁移至此）
- idx 使用全量序号（与 `_build_incremental_msg_text()` 当前行为一致，entity-extractor 改 task 方式后首次使用此格式）

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

## 边缘场景处理

### 首次运行

所有游标文件不存在时，游标值为空字符串。`_build_incremental_msg_text()` 中 `start_idx = 0`，生成全量消息。三个子Agent均从开头处理。与当前行为一致，无需额外处理。

### 游标指向已删除消息

用户或 Context Manager 可能删除了游标指向的消息。此时在消息列表中找不到游标 UUID，定位失败。

处理方式：游标定位失败时，回退到从头开始处理（`start_idx = 0`），等效于首次运行。不应保留旧游标导致死循环。

### 空增量范围

如果子Agent的增量范围为空（游标已在末尾且无新消息），跳过该子Agent调用。不传空消息列表给 LLM，避免无意义的 token 消耗。

### 边缘场景：force 模式 Dream Evolver 增量范围

force 模式下 Dream Evolver 仍为增量模式。增量范围可能很大（如果上次 sleep 时 dream 游标未推进），但**不需要截断**，因为子Agent使用 FIFO 模式管理上下文：当 token 超过 75% 阈值（150K），自动从最早的消息开始丢弃。因此 Dream Evolver 会优先处理最近的消息，最早的消息被 FIFO 自然丢弃。这与设计原则3（sleep/force 都只传增量）一致。

### 串行调用的双游标隔离

三个子Agent串行执行：Entity → Dream → Context Manager。隔离规则：

1. **每个子Agent执行前重新获取消息列表** — 前一个子Agent可能通过 `delete_messages`/`update_message` 修改了 DB，后续Agent必须看到最新状态
2. **每个子Agent独立计算增量范围** — 基于各自的游标和最新消息列表，与其它Agent的游标无关
3. **Context Manager 的上界使用 Dream 推进后的新游标** — 无论 Dream 正常推进还是 fallback，新游标都代表 Dream 已覆盖的范围

**Dream 与 Entity 的关系**：Dream 和 Entity 是独立的游标，不存在依赖关系。Dream 只负责增量范围内出现的新实体的精加工，不需要知道 Entity 做了什么。prompt 简化为"对以下消息中涉及的实体做精加工"，不再提及 Entity。

```
Entity Extractor:
  范围: [entity_cursor, 末尾]
  输入: 重新获取 messages → _build_incremental_msg_text(messages, entity_cursor)
  输出: 推进 entity_cursor → 写入游标文件

Dream Evolver:
  范围: [dream_cursor, 末尾]  （与 entity_cursor 无关）
  输入: 重新获取 messages → _build_incremental_msg_text(messages, dream_cursor)
  输出: 推进 dream_cursor → 写入游标文件
  记录: dream_cursor_new = 推进后的游标值（含 fallback）

Context Manager:
  范围: [compress_cursor, dream_cursor_new]
  输入: 重新获取 messages → _build_incremental_msg_text(messages, compress_cursor, end_cursor_id=dream_cursor_new)
  输出: 推进 compress_cursor → 写入游标文件
  注意: 上界是 Dream 推进后的游标，不包含 Dream 还未处理的消息
```

force 模式同理，只是 Entity Extractor 范围为全量（`entity_cursor = ""`），其余逻辑相同。

## 边缘场景处理

子Agent可能返回多种格式的游标：`"uuid"`、`null`、空字符串、带换行等。统一处理：
- `_extract_cursor_id()` 正则同时匹配 `"value"` 和 `null` 两种格式
- 返回 `null` 时走 fallback（推进到增量消息最后一条的 UUID）
- 返回空字符串或无效 UUID 时同样走 fallback
- 返回不在 `msg_id_set` 中的 UUID 时，视为无效，走 fallback

### 并发调用

auto-tidy 运行期间用户发送新消息，可能导致：
- 游标推进到新消息，但新消息尚未被子Agent处理
- 实际上当前 auto-tidy 是串行执行的（`_tidy_context_impl` 在主循环中运行），新消息会在下一轮 tidy 中处理
- 无需额外并发控制，但需确保游标推进后不会跳过未处理的消息

### Context Manager 保护数量配置

`~/.niu/preferences.json` 中如果没有 `context.protectRecentCount` 字段，默认值为 10。在 `_tidy_context_impl()` 中读取时使用 `prefs.get("context", {}).get("protectRecentCount", 10)`。无需初始化逻辑。

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
