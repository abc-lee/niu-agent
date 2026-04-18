# 用户长期记忆驻留设计

> 日期：2026-04-18
> 状态：设计完成

---

## 问题

当前记忆系统混乱：

| 机制 | 问题 |
|------|------|
| `memory.json` permanent | 启动时静态烘焙到 system prompt，运行中不更新 |
| handler.py 每5轮 recall | 从向量库搜L1层注入 next_prompt，但用户记忆已驻留时冗余 |
| handler.py 每10轮 global memory | 读不存在的文件，死代码 |
| `agent/memory/__init__.py` | 死模块，从未被导入 |
| `start_long_term_update` | 旧的AI自动提取记忆流程，与用户显式记忆职责不清 |
| dream-evolver | 写向量库（document/query_pattern/interaction_habit），与 memory-server/recall 的 memory_type 类别不互通 |

用户说"记住这个"的记忆应该**长期驻留 system prompt**，有变化时触发更新，不需要每5轮去搜一次。

## 设计

### 1. 存储：memory.json permanent 数组

```json
{
  "permanent": [
    "我喜欢Python，不喜欢JavaScript",
    "我的工作目录是E:/projects/main",
    "密码是abc123",
    "回复要简洁不要啰嗦",
    "每周五下午有例会"
  ]
}
```

**限制**：
- 最多 5 条
- 每条 ≤ 200 token（约 300 中文字符）
- 超限时主 Agent 必须先删旧的再加新的

**校验**：加载时若 `permanent` 数组长度 > 5，截断保留前 5 条（从末尾删），写回文件。

### 2. 注入：system prompt `### [用户长期记忆]` 段落

**启动时**：`get_system_prompt()` 读取 `permanent` 数组，注入 `### [用户长期记忆]` 段落到 base_system_prompt。

**运行时动态刷新**：
- `remember` / `forget` 工具执行后，设 `_memory_dirty = True`
- 下一轮 `_on_turn_end()` 检查 `_memory_dirty`，若为 True：
  1. 重新读取 `memory.json` 的 `permanent` 数组
  2. 用正则替换 `messages[0]["content"]` 中的 `### [用户长期记忆]\n...` 段落
  3. 设 `_memory_dirty = False`

**与 `_inject_dynamic_resources()` 的关系**：两者共存于 `messages[0]["content"]`，互不干扰。用户记忆是独立段落，不经过向量检索。

### 3. 工具接口

统一 `memory-server/` 前缀：

**`memory-server/remember`** — 添加用户长期记忆
- 参数：`content` (string, ≤200 token)
- 逻辑：
  1. 读取 `memory.json`
  2. 若 `permanent` 数组长度 ≥ 5，返回错误"记忆已满(5/5)，请先调用 memory-server/forget 删除旧记忆"
  3. 追加到 `permanent` 数组
  4. 写回 `memory.json`
  5. 设 `_memory_dirty = True`

**`memory-server/forget`** — 删除用户长期记忆
- 参数：`index` (int, 1-5) 或 `keyword` (string, 不区分大小写的子串匹配)
- 逻辑：
  1. 读取 `memory.json`
  2. 按 index 或 keyword 匹配删除
  3. 写回 `memory.json`
  4. 设 `_memory_dirty = True`

**`memory-server/list`** — 查看当前所有记忆
- 参数：无
- 返回：`permanent` 数组内容 + 当前条数/上限

### 4. 清理废弃逻辑

| 删除项 | 文件 | 原因 |
|--------|------|------|
| 每5轮 recall | `handler.py:566-569` | 用户记忆已驻留，不需要周期性向量检索 |
| 每10轮 global memory | `handler.py:583-591` | 读不存在的文件，死代码 |
| `agent/memory/__init__.py` | 整个文件 | 死模块，从未被导入 |
| `_should_remember()` | `handler.py:462-483` | 旧的自动提取流程，被 remember 工具替代 |
| `start_long_term_update` | `handler.py:937-993` | 旧的自动提取流程，被 remember 工具替代 |
| `suggest_remember` 注入 | `handler.py:572-579` | 配套 suggest_remember 的 [SYSTEM TIP] 注入 |

### 5. 保留不动的

| 保留项 | 原因 |
|--------|------|
| `_inject_dynamic_resources()` | 向量库动态注入（skill、mcp_tool、document、interaction_habit），dream-evolver 产出走此通道 |
| dream-evolver | 不受影响，继续写向量库 |
| memory-server 向量存储 | L0/L1/L2 能力保留给其他用途，用户显式记忆不再走向量库 |
| `memory.json` 身份/配置字段 | identity、workspace、user、firstRun 不变 |

### 6. 关键文件变更

| 文件 | 操作 |
|------|------|
| `mcp-servers/memory-server/src/niu_memory_server/__init__.py` | 修改：添加 remember/forget/list 工具，操作 memory.json permanent 数组 |
| `agent/runner.py` | 修改：get_system_prompt 注入 `### [用户长期记忆]`；_on_turn_end 加 dirty 刷新逻辑 |
| `agent/handler.py` | 修改：删除5轮recall、10轮global memory、suggest_remember、start_long_term_update |
| `agent/memory/__init__.py` | 删除：死模块 |
| `config/agents/niu.md` | 修改：更新记忆工具使用说明 |
