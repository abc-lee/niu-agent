# 非压缩子 Agent 改用 history 逐条传消息 + 去掉无用前缀 Implementation Plan (v1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 entity-extractor / dream-evolver / journal-agent 三个程序触发子 Agent 的上下文丢失 bug + 指令与内容混合导致的误执行风险。当前它们把 600 条消息拼成单条 task 字符串传给 `call_subagent_with_auto_answer`，被 `_truncate_task_for_subagent` 砍掉**末尾最新工作内容**（idx 508-600 共 92 条丢失），且每条消息前加了无用的 `[id:UUID] [idx:N] Ntokens role: ` 前缀（占 ~25000 tokens，20%+ 容量）。**更危险的是**：指令和消息内容混在单条 task 字符串里，截断后子 Agent 可能把消息内容里的"指令样"句子当成指令执行（日志管理子 Agent journal-agent 曾把主 Agent 遗留的"删除多余脑区"当指令执行了十几轮）。修复方向：改成像 context-manager 那样用 `history=list[message]` 逐条传消息 + `task` 作为独立指令消息 + 仿 context-manager 简易 ID 映射（history 每条 content 前缀极简 `[N]` 编号 + 程序内存维护 `简易ID↔UUID` 映射 + 子 Agent 回传 `processed_up_to=N` + 程序据此精确推进游标，带 `msg_ids[-1]` 兜底），**指令和内容彻底分离 + 保留三个子 Agent 各自的双游标机制**。改造覆盖三条路径共 10 个调用点：`niu_api/compat.py` 的 async `_tidy_context_impl`（sleep×3 + force×3 = 6 点）+ `agent/runner.py` 的 sync `_on_context_high_usage` force 镜像（3 点）+ `agent/handler.py` 的主 Agent 触发 journal-agent 路径（1 点）。

**Architecture:** 新增一个 `_build_plain_history` helper（构造带极简编号 `[N]` 前缀的 history 列表 + 同步构建 `简易ID↔UUID` 映射，仿 context-manager 的 `_build_compress_history` 但前缀极简且不排除 PROTECTED/孤立 tool），在三条路径共 9 个调用点替换原"task 字符串嵌入消息"模式为"history 逐条 + task 独立指令"：

### 简易 ID 映射机制（仿 context-manager，核心新增）

**背景**：当前三个子 Agent 的游标由**程序自动推进**（`new_id = msg_ids[-1]`，假设"调用成功=全部处理完"），子 Agent 不回传任何"处理到哪了"的标记。用户要求仿照 context-manager 的简易 ID 映射机制，让子 Agent 回传处理进度，程序据此更新游标——解决"部分处理"场景（子 Agent 中途停止但未 overflow，或主动只处理到某条）。

**机制**（与 context-manager 同构）：
1. **程序内存维护 `简易ID↔UUID` 映射**：`_build_plain_history` 构造 history 时同步返回 `idx_to_id: dict[int, str]`（`{1: "uuid-aaa", 2: "uuid-bbb", ...}`），key 是简易编号（1-based），value 是真实 message UUID
2. **history 每条 content 前缀极简编号**：`[1] 消息内容` / `[2] 消息内容`（**不是** `[id:UUID] [idx:N] Ntokens role: ` 那种臃肿格式，也不是 context-manager 的 `[idx:N] Ntokens `，就是极简 `[N] `）
3. **子 Agent 回传处理进度**：在最终回复中输出一行 `processed_up_to=N`（N 是简易编号），表示"我已处理到第 N 条"
4. **程序解析 + 更新游标**：程序从子 Agent 输出中正则提取 `processed_up_to=N`，查 `idx_to_id[N]` 得到真实 UUID，写入游标 JSON
5. **兜底**：如果子 Agent 没输出 `processed_up_to=`（格式不符或 LLM 没遵循），程序回退到原逻辑 `msg_ids[-1]`（保证不丢游标推进）

**与 context-manager 的区别**：
- context-manager 前缀 `[idx:N] Ntokens `（含 token 标注，因为要做压缩决策需要看 token 占比）；非压缩子 Agent 前缀 `[N] `（极简，只给编号）
- context-manager 子 Agent 输出 `keep=`/`update=`/`cursor=`（操作 DB 删/改消息）；非压缩子 Agent 输出 `processed_up_to=N`（只更新游标，不操作 DB）
- context-manager 排除 PROTECTED + 孤立 tool（idx 必须连续以便 keep= 解析）；非压缩子 Agent 不排除（所有消息都该看到，子 Agent 自己判断，且 `processed_up_to=` 只需编号存在不需连续）

### 双游标动态区间说明（不全量传消息，每个子 Agent 各自游标）

**用户铁律：三个非压缩子 Agent 都有各自的双游标机制，改造必须保留**。因为它们都是非破坏性的，每次提取后下次上下文可能还带着上次已提取过的内容，需要各自的游标标记"上次处理到哪了"。

三个子 Agent 的游标字段（已代码确认）：
- **entity-extractor**：`last_entity_extract_id`（值=message UUID）+ `last_entity_extract_at`（时间戳），存 `~/.niu/last_entity_extract.json`
- **dream-evolver**：`last_dream_evolve_id` + `last_evolve_at`，存 `~/.niu/last_dream_evolve.json`
- **journal-agent**：`last_journal_id` + `last_journal_at`，存 `~/.niu/last_journal.json`

区间策略：
- **sleep 模式**：
  - entity-extractor：`last_entity_extract_id` 游标之后的新消息（增量）
  - dream-evolver：`last_dream_evolve_id` 游标之后的新消息（增量）
  - journal-agent：`last_journal_id` 游标之后的新消息（增量）
- **force 模式**：
  - entity-extractor：`cursor=""` 全量（force 模式重新提取所有消息的实体）
  - dream-evolver：`last_dream_evolve_id` 游标之后的新消息（增量）
  - journal-agent：`last_journal_id` 游标之后的新消息（增量）
- **handler.py 主 Agent 触发 journal-agent**：`last_journal_id` 游标之后的新消息（增量），游标为空且消息过多时限制为最近 200 条

**改造保留游标逻辑，只改推进方式 + 传递方式**：
- 推进方式：从"程序自动 `msg_ids[-1]`"改为"子 Agent 回传 `processed_up_to=N` → 程序查映射 → 写游标"（带 `msg_ids[-1]` 兜底）
- 传递方式：从"区间内消息拼成 task 字符串"改为"区间内消息作为 history 逐条传 + 极简 `[N]` 前缀"
- 区间由 `_build_incremental_msg_text(messages, cursor_id, out_msg_ids, msg_tokens)` 计算（它按游标 ID 定位起点的下一条，一直取到消息列表末尾），改造后仍调用它收集 `out_msg_ids`（游标推进 + 映射构建依赖），但**丢弃返回的文本**，改用 `_build_plain_history(incremental_msgs)` 构造 history + 映射

1. **`niu_api/compat.py` 的 `_tidy_context_impl`**（async）：sleep 模式 3 调用点（entity/dream/journal）+ force 模式 3 调用点（entity/dream/journal）= 6 个
2. **`agent/runner.py` 的 `_on_context_high_usage`**（sync，compat.py force 模式的同步镜像）：force 模式 3 调用点（entity/dream/journal）= 3 个，需扩展 `_run_subagent_step` 签名透传 `history` / `context_fifo_threshold`
3. **`agent/handler.py` 的 `_build_journal_task_for_handler`**（主 Agent 通过 `chat-with-journal-agent` 触发 journal-agent 的路径）：1 个，需改为返回 `(task, history, idx_to_id, msg_ids)` 四元组，并改造 `_update_journal_cursor` 用 `_parse_processed_up_to` + 查映射更新游标（带兜底）

同时简化三个子 Agent 的 system prompt，去掉对 `[id:UUID] [idx:N]` 格式的描述（它们不再需要坐标，直接读 history 即可）。context-manager 完全不动，`_build_incremental_msg_text` 和 `_truncate_task_for_subagent` 保留（前者仍被 context-manager 模式一 + 旧 incremental 路径用 + 本次各调用点仍用它收集 `out_msg_ids`，后者仍被 context-manager 模式一用）。

### 核心原则：指令与消息内容彻底分离（用户铁律）

**背景**：日志管理子 Agent（journal-agent）被截断后，曾把主 Agent 遗留的"删除多余脑区"那句话（本是对话内容）**当成了指令**，开始十几轮删除脑区的行为。根因是非压缩子 Agent 当前把指令和消息内容混在单条 task 字符串里，截断时砍掉末尾（最新消息），更危险的是——如果消息内容里恰好有"指令样"的句子，子 Agent 会把它当成指令执行。

**改造原则**（所有非压缩子 Agent 必须遵守）：
- **task 是独立指令消息**：只含工作指令（如"从中提取有价值的内容..."），**不含任何消息内容**
- **history 是消息内容列表**：每条 `{"role":..., "content":...}`，content 前缀极简编号 `[N] `（用于子 Agent 回传 `processed_up_to=N`），**不含任何指令**
- **指令和内容彻底分离**：指令不会被截断（task 本身只有 ~500 字符），内容也不会被误当指令
- **复用 context-manager 的现成方法**：`call_subagent_with_auto_answer(task=纯指令, history=list[message], context_fifo_threshold=0)` 已证实可行

### 双游标动态区间说明（每个子 Agent 各自游标，详见上方"双游标动态区间说明"段）

游标字段 + 区间策略 + 推进方式改造详见 Architecture 段的"双游标动态区间说明"。核心：三个子 Agent 各自独立游标（entity 用 `last_entity_extract_id`、dream 用 `last_dream_evolve_id`、journal 用 `last_journal_id`），改造保留游标逻辑，只改推进方式（程序自动→子 Agent 回传 `processed_up_to=N`）+ 传递方式（task 字符串→history 逐条 + `[N]` 前缀）。

**Tech Stack:** Python 3.11+，pytest，subagent 同步调用架构

---

## Context

### 当前 bug

程序触发子 Agent 调用链路（`_tidy_context_impl` 的 sleep 模式 + force 模式）：

1. 主 Agent 上下文超阈值（80%+），触发 force 模式压缩
2. force 模式按顺序跑 entity-extractor → dream-evolver → journal-agent → context-manager
3. entity-extractor 收到的 task 是一条巨型字符串：开头是指令（~500 字符），中间是 `_build_incremental_msg_text` 拼接的 600 条消息文本（每条前缀 `[id:UUID] [idx:N] Ntokens role: `）
4. 这条 task 字符串总 token 数远超 `safe_tokens = context_window * 0.6`（默认 120K * 0.6 = 72K，但 task 实际 150K+）
5. `_truncate_task_for_subagent` 截断 task 保留**开头**（指令 + 第一份消息），砍掉**末尾**（第三份 = 最新工作内容）
6. 子 Agent 看不到最新的工作内容，提取/精加工/日志全失败或质量极低

### 日志证据

- `logs/raw_http/20260709/000030_request.json`（entity）：task 单条字符串，被截断
- `logs/raw_http/20260709/000031_request.json`（dream）：同上
- `logs/raw_http/20260709/000060_request.json`（journal）：同上
- `logs/raw_http/20260709/000062_request.json`（context-manager 正确范例）：history 逐条传，未截断

### 三个问题

1. **task 字符串嵌入消息触发截断**：600 条消息拼成单条 task，超过 30K 字符（120K tokens）截断阈值，丢失末尾最新工作内容
2. **指令没独立成消息**：指令嵌在 task 字符串开头，跟消息列表混在一起不清晰（虽然没被截断，但结构不优雅）
3. **无用 UUID/idx 前缀浪费容量**：每条消息前加 `[id:UUID] [idx:N] Ntokens role: ` 共 ~42 字符，600 条 = ~25000 tokens，占 20%+ 容量。这三个子 Agent 不操作 message.DB，不需要坐标

### 修复方向（用户锁定）

非压缩子 Agent（entity-extractor / dream-evolver / journal-agent）改成像 context-manager 那样：
- 用 `history=list[message]` 逐条传消息
- `task` 作为独立指令消息
- 去掉无用的 UUID/idx 前缀（它们不操作 message.DB）

### 关键代码位置（HEAD = 27b287f4）

| 文件 | 行号 | 内容 | 改动 |
|------|------|------|------|
| `niu_api/compat.py` | L397-500 | `_build_compress_history`（context-manager 用，加 `[idx:N] Ntokens ` 前缀） | **不动** |
| `niu_api/compat.py` | L311-394 | `_build_incremental_msg_text`（非压缩子 Agent 当前用，加 `[id:UUID] [idx:N] Ntokens role: ` 前缀） | **不动**（保留给其他可能的调用者，且 force 路径仍用它收集 `out_msg_ids`） |
| `niu_api/compat.py` | 新增 | `_build_plain_history(messages, out_msg_ids) -> (history, idx_to_id)` — 构造带 `[N]` 极简前缀的 history + 简易ID↔UUID 映射 | **新增** |
| `niu_api/compat.py` | 新增 | `_parse_processed_up_to(response: str) -> int | None` — 正则提取子 Agent 输出中的 `processed_up_to=N`，返回 N 或 None | **新增** |
| `niu_api/compat.py` | L853-869 | `_build_journal_task`（journal task 构造，内嵌 msg_text） | **改为 task 独立指令（含"消息以 `[N]` 编号，处理完回复 `processed_up_to=N`"说明），不再嵌入 msg_text** |
| `niu_api/compat.py` | L1842-1868 | sleep 模式 entity 调用点 | **改为 history 逐条 + task 独立** |
| `niu_api/compat.py` | L1920-1941 | sleep 模式 dream 调用点 | **改为 history 逐条 + task 独立** |
| `niu_api/compat.py` | L1996-2012 | sleep 模式 journal 调用点 | **改为 history 逐条 + task 独立** |
| `niu_api/compat.py` | L2548-2569 | force 模式 entity 调用点 | **改为 history 逐条 + task 独立** |
| `niu_api/compat.py` | L2620-2641 | force 模式 dream 调用点 | **改为 history 逐条 + task 独立** |
| `niu_api/compat.py` | L2696-2711 | force 模式 journal 调用点 | **改为 history 逐条 + task 独立** |
| `agent/runner.py` | L942-985 | `_run_subagent_step`（force 镜像的子 Agent 调用封装，内部调 `call_subagent_with_auto_answer`，**不接 history**） | **扩展签名加 `history=None, context_fifo_threshold=None`，透传到 `call_subagent_with_auto_answer`** |
| `agent/runner.py` | L1108-1126 | force 镜像 entity 调用点（同步版，调 `_run_subagent_step`） | **改为 history 逐条 + task 独立** |
| `agent/runner.py` | L1144-1162 | force 镜像 dream 调用点（同步版） | **改为 history 逐条 + task 独立** |
| `agent/runner.py` | L1178-1193 | force 镜像 journal 调用点（同步版，调 `_build_journal_task(journal_force_msg_text, safe_tokens)`） | **改为 history 逐条 + task 独立** |
| `agent/handler.py` | L824-882 | `_build_journal_task_for_handler`（主 Agent 通过 `chat-with-journal-agent` 触发 journal-agent，调 `_build_journal_task(journal_msg_text, safe_tokens)` 返回 `(task, journal_msg_ids)`） | **改为返回 `(task, history, idx_to_id, msg_ids)` 四元组，task 用 `_build_journal_task()`，history + idx_to_id 用 `_build_plain_history`** |
| `agent/handler.py` | L884-937 | `_update_journal_cursor`（从 journal-agent 结果提取游标并更新，当前用 `msg_ids[-1]` 自动推进） | **扩展签名加 `journal_idx_to_id` 参数 + 用 `_parse_processed_up_to` 解析 + 查映射更新游标（带 `msg_ids[-1]` 兜底）** |
| `agent/handler.py` | L939-1010 | `_call_subagent_gen`（调用 `_build_journal_task_for_handler` 拿 task，传给 `call_subagent`） | **同步接收 history + idx_to_id，透传给 `call_subagent` 的 `history=` 参数 + `_update_journal_cursor` 的 `journal_idx_to_id` 参数** |
| `config/agents/entity-extractor.md` | L23-29, L78-82 | 输入规范 + 游标机制段描述 `[id:UUID] [idx:N]` 格式 | **改为描述 history 逐条 + `[N]` 极简前缀 + `processed_up_to=N` 回传** |
| `config/agents/dream-evolver.md` | L474-493 | 游标机制段描述 `[id:UUID] [idx:N] Xtokens role:` 格式 | **改为描述 history 逐条 + `[N]` 极简前缀 + `processed_up_to=N` 回传** |
| `config/agents/journal-agent.md` | L25-32, L74-81 | 输入格式 + 游标机制段描述 `[id:UUID] [idx:N]` 格式 | **改为描述 history 逐条 + `[N]` 极简前缀 + `processed_up_to=N` 回传** |
| `niu_api/compat.py` | L2805-2818 | context-manager force 调用点（正确范例） | **不动** |
| `niu_api/compat.py` | L2174-2182 | context-manager 模式二调用点（正确范例） | **不动** |

### 设计决策

#### 1. 新增 `_build_plain_history`（带极简编号 + 简易ID↔UUID 映射，仿 context-manager）

**理由**：
- `_build_compress_history` 加 `[idx:N] Ntokens ` 前缀 + 排除 PROTECTED + 排除孤立 tool，是 context-manager 专用语义（它要按 idx 解析 `keep=/update=` 写 DB）
- 三个非压缩子 Agent 不操作 DB，但**需要简易编号让子 Agent 回传 `processed_up_to=N`**（用户铁律：仿 context-manager 简易 ID 映射）
- 前缀极简 `[N] `（不是 `[idx:N] Ntokens `，不是 `[id:UUID] [idx:N] Ntokens role: `），只给编号不给 UUID/tokens/role
- 不排除 PROTECTED（所有消息都该看到，包括受保护的近期消息）— **例外：entity force 全量路径在调用 `_build_plain_history` 前由调用方过滤 PROTECTED**（方案 A，详见 Architecture §6），因为 force 全量 600 条消息进 history 会 overflow 死循环；sleep 增量路径不排除（消息数少，无 overflow 风险）
- 不排除孤立 tool（保持原顺序，子 Agent 自己判断）

**`_build_plain_history` 签名**：
```python
def _build_plain_history(messages, out_msg_ids: list | None = None) -> tuple[list[dict], dict[int, str]]:
    """构造带 [N] 极简前缀的 history 列表 + 简易ID↔UUID 映射（仿 context-manager 的 _build_compress_history）。

    用于非压缩子 Agent（entity-extractor / dream-evolver / journal-agent）的 force/sleep 调用：
    - history 每条 content 前缀 "[N] "（N 是 1-based 简易编号）
    - 同步构建 idx_to_id 映射 {N: 真实UUID}，供程序解析子 Agent 输出的 processed_up_to=N 后查 UUID 更新游标
    - 不排除 PROTECTED 消息（所有消息都该看到）
    - 不排除孤立 tool（保持原顺序，子 Agent 自己判断）

    与 _build_compress_history 的区别：
    - 前缀极简 "[N] "（不是 "[idx:N] Ntokens "）
    - 不排除 PROTECTED / 不排除孤立 tool（调用方按需在调用前过滤 PROTECTED，如 entity force 全量路径，详见 Architecture §6）
    - 不含 token 标注（非压缩子 Agent 不需要做压缩决策）

    Args:
        messages: 全量消息列表（Message 对象，含 id/role/content/tool_calls/tool_call_id）
        out_msg_ids: 输出参数，收集消息的真实 ID 列表（与 history 等长同顺序，用于游标推进兜底）

    Returns:
        (history, idx_to_id):
        - history: [{"role":..., "content": "[N] 原content", "tool_calls"?:..., "tool_call_id"?:...}, ...]
        - idx_to_id: {N: 真实 message_id}，用于解析子 Agent 输出的 processed_up_to=N
    """
```

#### 1.5. 新增 `_parse_processed_up_to`（解析子 Agent 回传的处理进度）

**理由**：子 Agent 在最终回复中输出 `processed_up_to=N` 表示"已处理到第 N 条"，程序需正则提取 N，查 `idx_to_id[N]` 得到 UUID 更新游标。如果提取不到（LLM 没遵循格式或格式不符），返回 None，调用方回退到 `msg_ids[-1]` 兜底（保证不丢游标推进）。

**签名**：
```python
def _parse_processed_up_to(response: str) -> int | None:
    """从子 Agent 输出中提取 processed_up_to=N 的 N 值。

    支持格式（大小写不敏感）：
    - "processed_up_to=15"
    - "processed_up_to: 15"
    - "processed_up_to 15"
    - 匹配第一个有效整数

    Returns:
        N (int) 或 None（未找到）
    """
```

**游标推进新逻辑**（替换原 `new_id = msg_ids[-1]`）：
```python
# 解析子 Agent 回传的 processed_up_to=N
_processed_idx = _parse_processed_up_to(result)
if _processed_idx is not None and _processed_idx in idx_to_id:
    new_id = idx_to_id[_processed_idx]
    logger.info(f"[Tidy] {step_name} cursor advanced per processed_up_to={_processed_idx} -> {new_id}")
elif msg_ids:  # 兜底：子 Agent 没输出 processed_up_to=，回退到原逻辑
    new_id = msg_ids[-1]
    logger.info(f"[Tidy] {step_name} processed_up_to not found, fallback to range end: {new_id}")
else:
    new_id = last_cursor_id  # 没有消息，游标不动
```

#### 2. `_build_incremental_msg_text` 保留但不再用于构造 task

**理由**：
- 仍需用它收集 `out_msg_ids`（游标推进依赖这个列表）
- force 模式的 entity 用 `cursor=""` 全量收集，dream/journal 用各自游标增量收集
- 改造后：`_build_incremental_msg_text` 只用来算 `out_msg_ids`，**不再用它的文本结果**构造 task

**优化**：可以新增一个轻量 helper `_collect_msg_ids_in_range(messages, last_cursor_id, end_cursor_id=None) -> list[str]` 只收集 ID 不拼文本，但为了最小化改动 + 复用已测过的游标定位逻辑，**本次仍调用 `_build_incremental_msg_text` 但丢弃返回的文本**，只取 `out_msg_ids`。后续可优化。

#### 3. task 指令独立成消息，不再嵌入消息文本

**改造前**（entity force 范例）：
```python
entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容...

{entity_force_msg_text}"""  # ← 600 条消息文本嵌在这里

truncated = _truncate_task_for_subagent(entity_force_prompt, safe_tokens)
call_subagent_with_auto_answer(task=truncated, history=None)
```

**改造后**：
```python
entity_force_prompt = """以下是最近的对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，N 是 1-based 序号）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""

entity_force_history, entity_idx_to_id = _build_plain_history(messages)
# entity_idx_to_id = {1: "uuid-aaa", 2: "uuid-bbb", ...}  # 程序内存维护

call_subagent_with_auto_answer(
    task=entity_force_prompt,           # ← 纯指令，不含消息
    history=entity_force_history,       # ← 消息逐条传，content 前缀 [N]
    context_fifo_threshold=0,           # ← 关闭 FIFO，保留完整上下文（与 context-manager 一致）
)

# 调用后解析输出 + 更新游标
_processed_idx = _parse_processed_up_to(result)
if _processed_idx is not None and _processed_idx in entity_idx_to_id:
    new_entity_id = entity_idx_to_id[_processed_idx]
elif entity_force_msg_ids:
    new_entity_id = entity_force_msg_ids[-1]  # 兜底
else:
    new_entity_id = last_entity_extract_id
```

#### 4. 去掉 `_truncate_task_for_subagent` 调用

**理由**：
- task 不再嵌入消息文本，本身只有 ~500 字符，远低于截断阈值
- history 逐条传，单条 message 不会超限（每条就是原大小）
- 子 Agent 的 `context_fifo_threshold=0` 关闭 FIFO，由 LLM 上下文窗口自然限制（与 context-manager 一致）

**保留 `_truncate_task_for_subagent`**：context-manager 模式一仍用它截断 `compress_msg_text`（L2096），不能删。

#### 5. system prompt 同步修改

三个子 Agent 的 system prompt 里对 `[id:UUID] [idx:N]` 格式的描述要改为"消息以 history 形式逐条传入，每条 content 前缀 `[N]` 极简编号（1-based），处理完成后在最终回复最后一行输出 `processed_up_to=N`"。

**保留的描述**：
- 游标机制（程序只传增量消息）
- role 字段含义（user/assistant/tool）
- 消息内容为完整原文
- **新增**：`[N]` 编号含义 + `processed_up_to=N` 回传格式

**删除的描述**：
- `[id:UUID]` 字段（不再出现）
- `[idx:N]` 字段（不再出现，改为 `[N]` 极简编号）
- `Xtokens` 字段（不再出现）

#### 6. entity force 全量 history 的 overflow 死循环风险 + 缓解（方案 A：排除 PROTECTED）

**背景**：entity force 模式（`_tidy_context_impl` force 1/3 + runner force 镜像 entity）用 `cursor=""` 全量收集所有消息（约 600 条）。改造前用 `_truncate_task_for_subagent` 截断 task 字符串保留前半部分（虽丢最新工作内容但避免 overflow）；**改造后改用 `history=` 逐条传 + `context_fifo_threshold=0`，无任何截断兜底**。如果 600 条消息总 token 超过子 Agent 上下文窗口：
- `agent_runner_loop`（agent/generic/agent_loop.py:757-772）检测到 `context_length_exceeded`，返回 `CONTEXT_OVERFLOW`
- `_is_subagent_overflow(entity_result)` 返回 True，游标不动（不推进）
- 下次 force 触发时相同 600 条消息再进 history → 再次 overflow → 游标再不动 → **死循环**

**代码调查结论**（确认死循环风险真实存在）：
- `agent_runner_loop` (L757-772) 检测 LLM `context_overflow` 标记，返回 `CONTEXT_OVERFLOW`（不崩溃，数据不丢，但游标不推进）
- entity force 当前路径 `niu_api/compat.py:2548-2570` 用 `_truncate_task_for_subagent` 截断字符串，是当前唯一防 overflow 机制；改造后该调用被移除（Task 6.2），history 逐条传无截断
- `_truncate_tool_content` (L572) 只对单条 tool 消息 content 截断到 30000 字符，不限制总消息条数
- context-manager 用同样 `context_fifo_threshold=0` 跑 600 条消息不 overflow，因为 `_build_compress_history` **排除 PROTECTED（最近 10 条）+ 排除孤立 tool**，消息数显著减少；entity force 用 `_build_plain_history` 不排除任何消息，全量 600 条
- entity force 触发时机是上下文 >80%，此时 600 条消息很可能已接近窗口上限，全量进 history overflow 风险高

**缓解方案**：entity force 调用点（Task 6 / Task 10.2）在调用 `_build_plain_history(messages)` 前，**先排除 PROTECTED 消息（最近 10 条）**，与 context-manager 对齐。这样：
- 减少消息数（600 → 590），降低 overflow 风险
- PROTECTED 是最近的消息，下次 sleep 模式 entity 增量运行会自动覆盖（游标推进后下次增量含这 10 条）
- 与 context-manager 的 PROTECTED 排除逻辑一致，架构统一
- 不影响 entity force 的"重新提取所有消息实体"语义（590 条 vs 600 条差异微小，且最近 10 条本就在 sleep 增量覆盖范围内）

**实现细节**（Task 6 / Task 10.2 应用）：
- 从 `niu_api/compat.py` 复用 context-manager 已有的 PROTECTED 计算逻辑（最近 N 条 user/assistant 消息 ID 集合，N 由 `_read_protect_recent_count()` 读取）
- force 块顶部计算 `_force_protected_ids`（变量名带 `_force_` 前缀避免与 sleep 模式的 `protected_ids` 混淆）
- 在 `_build_plain_history(messages)` 前过滤：`entity_force_msgs_filtered = [m for m in messages if (getattr(m, "id", "") or "") not in _force_protected_ids]`
- 同步过滤 `entity_force_msg_ids`（游标推进兜底用），保证与 history 保持一致
- 注意：PROTECTED 阈值与 context-manager 保持一致（通常最近 10 条，从 `_read_protect_recent_count()` 读取）

**为什么不选其他方案**：
- 方案 B（限制最大条数分段处理）：需要改子 Agent 协议（多次调用 + 合并结果），改动大，违背"最小化改动"
- 方案 C（靠子 Agent 自己分段）：依赖 LLM 遵循指令，不可靠
- 方案 D（文档化不处理）：死循环风险真实存在，不能放任

**适用范围**：仅 entity force 全量路径（compat force entity + runner force 镜像 entity）。dream/journal 的 sleep + force 路径都是增量（游标之后的新消息，通常远少于 600 条），无 overflow 风险，不排除 PROTECTED。

### 关键约束（用户铁律）

- **修改前必须先做临时提交备份**（铁律 #3）— Task 0 做备份
- **禁止 `git reset --hard` / force push**（铁律 #9）
- **测试必须用真实数据 + 真实 LLM**（铁律 #5）— Task 6 用真实 ./niu 触发压缩
- **修改前必须用 gitnexus 分析影响范围**（铁律 #4）— 子 Agent 执行时跑
- **git 操作后必须修文件权限**（铁律 #7）— Task 7 修权限
- **派出去的子 Agent 必须遵守所有铁律**

---

## File Structure

```
ai-bot/                              # 项目根
├── niu_api/
│   └── compat.py                    # 改 6 个调用点 + 新增 _build_plain_history + 改 _build_journal_task
├── agent/
│   ├── handler.py                   # 改 _build_journal_task_for_handler（返回四元组）+ _update_journal_cursor（解析 processed_up_to）+ _call_subagent_gen（透传 history + idx_to_id）
│   └── runner.py                    # 改 force 镜像 3 个调用点 + 扩展 _run_subagent_step 签名
├── config/agents/
│   ├── entity-extractor.md          # 改输入规范 + 游标机制段
│   ├── dream-evolver.md             # 改游标机制段
│   └── journal-agent.md             # 改输入格式 + 游标机制段 + 输出格式段 + 写入流程段 L60 UUID 去重
├── tests/
│   ├── test_noncompress_subagent_history.py  # 新增，TDD 失败测试
│   ├── test_compress_history.py     # 已有，回归验证（不动）
│   ├── test_journal_agent_tidy.py   # 已有，回归验证
│   └── test_one_turn_compress.py    # 已有，回归验证
└── docs/superpowers/plans/
    └── 2026-07-09-noncompress-subagent-history-based.md  # 本计划
```

---

## Tasks

### Task 0: 修改前临时备份提交

- [ ] **Step 0.1**：检查工作区干净（除本次新计划文件外）
```bash
cd <repo_root>
git status
```
- [ ] **Step 0.2**：临时备份提交（标注问题名+节点类型+基线 hash）
```bash
cd <repo_root>
git add -A
git commit -m "backup: 非压缩子 Agent 改用 history 逐条传消息改造前临时备份 (baseline 27b287f4)

问题：entity-extractor/dream-evolver/journal-agent 把 600 条消息拼成单条 task 字符串，
被 _truncate_task_for_subagent 砍掉末尾最新工作内容（idx 508-600 共 92 条丢失），
且每条消息前加了无用的 [id:UUID] [idx:N] Ntokens role: 前缀（占 20%+ 容量）。
更危险的是：指令和消息内容混在 task 字符串里，截断后子 Agent 可能把消息内容里的
"指令样"句子当成指令执行（journal-agent 曾把"删除多余脑区"当指令执行了十几轮）。

准备改：
1. niu_api/compat.py 新增 _build_plain_history helper（带 [N] 极简前缀 history + idx_to_id 映射）+ _parse_processed_up_to helper（解析子 Agent 输出的 processed_up_to=N）
2. compat.py 6 个调用点（sleep×3 + force×3）改为 history 逐条 + task 独立指令 + 解析 processed_up_to 更新游标（带 msg_ids[-1] 兜底）
3. agent/runner.py force 镜像 3 个调用点同步改造 + 扩展 _run_subagent_step 签名透传 history + idx_to_id
4. agent/handler.py _build_journal_task_for_handler 改为返回 (task, history, idx_to_id, msg_ids) 四元组
   + _update_journal_cursor 用 _parse_processed_up_to 解析 + 查映射更新游标
   + _call_subagent_gen 透传 history + idx_to_id（主 Agent 触发 journal-agent 路径）
5. _build_journal_task 改为纯指令构造器（无参），不再嵌入 msg_text
6. 三个子 Agent 的 system prompt 同步改为 [N] 极简编号 + processed_up_to=N 回传格式
   + journal-agent.md:60 UUID 去重改为内容去重
7. context-manager 完全不动

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 1: TDD — 先写失败测试

**目标**：用 pytest 写 8 个测试覆盖 (1) `_build_plain_history` 基本构造 + 返回元组 (2) `_build_plain_history` 保留 tool_calls/tool_call_id (3) `_build_plain_history` content 前缀 `[N] ` 极简编号 (4) `_build_plain_history` 返回 idx_to_id 映射正确 (5) `_parse_processed_up_to` 解析各种格式 (6) `_parse_processed_up_to` 未找到返回 None (7) entity force 调用用 history 不用 task 嵌入 (8) dream/journal force 同上。

- [ ] **Step 1.1**：创建测试文件 `tests/test_noncompress_subagent_history.py`
```python
"""非压缩子 Agent（entity-extractor/dream-evolver/journal-agent）改用 history 逐条传消息的单元测试。

背景：这三个子 Agent 原本把 600 条消息拼成单条 task 字符串传给 call_subagent_with_auto_answer，
被 _truncate_task_for_subagent 砍掉末尾最新工作内容，且每条消息前加了无用的 [id:UUID] [idx:N] 前缀。
本次改造仿 context-manager 简易 ID 映射：history 每条 content 前缀 [N] 极简编号，
程序内存维护 idx_to_id 映射，子 Agent 回传 processed_up_to=N，程序查映射更新游标。

本测试验证：
1. _build_plain_history 构造带 [N] 前缀的 history + 返回 idx_to_id 映射
2. _build_plain_history 保留 tool_calls/tool_call_id
3. content 前缀是 [N] 极简编号（不是 [id:UUID] [idx:N] Ntokens role:）
4. _parse_processed_up_to 解析各种格式（= / : / 空格，大小写不敏感）
5. _parse_processed_up_to 未找到返回 None
6. entity/dream/journal 三个子 Agent 的 force 调用用 history=... 而非 task=巨型字符串
"""
from unittest import mock
import sys
from pathlib import Path

_root = Path(__file__).parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from niu_api.compat import _build_plain_history, _parse_processed_up_to


class FakeMsg:
    """模拟 Message 对象（compat.py 用 getattr(msg, 'id') 等访问）。"""
    def __init__(self, id, role, content, tool_calls=None, tool_call_id=None):
        self.id = id
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


def test_build_plain_history_basic_and_idx_to_id():
    """基本场景：3 条消息构造 history，content 前缀 [N] + 返回 idx_to_id 映射。"""
    messages = [
        FakeMsg(id="uuid-1", role="user", content="你好"),
        FakeMsg(id="uuid-2", role="assistant", content="你好，我是 Niu"),
        FakeMsg(id="uuid-3", role="user", content="今天天气"),
    ]
    out_msg_ids = []
    history, idx_to_id = _build_plain_history(messages, out_msg_ids=out_msg_ids)

    assert len(history) == 3
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "[1] 你好"  # 极简编号前缀
    assert history[1]["content"] == "[2] 你好，我是 Niu"
    assert history[2]["content"] == "[3] 今天天气"
    assert out_msg_ids == ["uuid-1", "uuid-2", "uuid-3"]
    # idx_to_id 映射：1-based 简易编号 -> 真实 UUID
    assert idx_to_id == {1: "uuid-1", 2: "uuid-2", 3: "uuid-3"}


def test_build_plain_history_preserves_tool_calls():
    """assistant 带 tool_calls + tool 消息：保留 tool_calls/tool_call_id，content 前缀 [N]。"""
    messages = [
        FakeMsg(id="msg-1", role="user", content="查天气"),
        FakeMsg(
            id="msg-2", role="assistant", content="",
            tool_calls=[{"id": "tc-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
        ),
        FakeMsg(id="msg-3", role="tool", content="今天晴", tool_call_id="tc-1"),
    ]
    history, idx_to_id = _build_plain_history(messages)

    assert len(history) == 3
    assert history[0]["content"] == "[1] 查天气"
    assert history[1]["tool_calls"] == [{"id": "tc-1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]
    assert history[1]["content"] == "[2] "  # 空 content 前缀 [2]
    assert history[2]["role"] == "tool"
    assert history[2]["tool_call_id"] == "tc-1"
    assert history[2]["content"] == "[3] 今天晴"
    assert idx_to_id == {1: "msg-1", 2: "msg-2", 3: "msg-3"}


def test_build_plain_history_prefix_is_minimal_not_verbose():
    """content 前缀是极简 [N]，不是 [id:UUID] / [idx:N] / Ntokens / role:。"""
    messages = [
        FakeMsg(id="uuid-abc-123", role="user", content="测试内容"),
    ]
    history, _ = _build_plain_history(messages)

    content = history[0]["content"]
    assert content == "[1] 测试内容"  # 极简 [N] 前缀
    assert "[id:" not in content
    assert "[idx:" not in content
    assert "tokens" not in content
    assert "role:" not in content
    assert "uuid-abc-123" not in content  # UUID 不出现在 content 里


def test_parse_processed_up_to_various_formats():
    """_parse_processed_up_to 支持 = / : / 空格分隔，大小写不敏感。"""
    assert _parse_processed_up_to("处理完成\nprocessed_up_to=15") == 15
    assert _parse_processed_up_to("processed_up_to: 15") == 15
    assert _parse_processed_up_to("processed_up_to 15") == 15
    assert _parse_processed_up_to("PROCESSED_UP_TO=15") == 15
    assert _parse_processed_up_to("Processed_Up_To=15") == 15
    # 匹配第一个有效整数
    assert _parse_processed_up_to("processed_up_to=3\nprocessed_up_to=15") == 3


def test_parse_processed_up_to_not_found_returns_none():
    """未找到 processed_up_to= 时返回 None。"""
    assert _parse_processed_up_to("处理完成，无标记") is None
    assert _parse_processed_up_to("") is None
    assert _parse_processed_up_to("processed_up_to=") is None  # 无数字
    assert _parse_processed_up_to("processed_up_to=abc") is None  # 非整数


def test_build_plain_history_empty_messages():
    """空消息列表返回空 history + 空 idx_to_id。"""
    history, idx_to_id = _build_plain_history([])
    assert history == []
    assert idx_to_id == {}


def test_build_plain_history_out_msg_ids_default_none():
    """out_msg_ids=None 时不报错（内部初始化为空列表）。"""
    messages = [FakeMsg(id="m1", role="user", content="hi")]
    history, idx_to_id = _build_plain_history(messages)  # 不传 out_msg_ids
    assert len(history) == 1
    assert idx_to_id == {1: "m1"}
```

- [ ] **Step 1.2**：跑测试确认全部失败（`_build_plain_history` / `_parse_processed_up_to` 还不存在）
```bash
cd <repo_root>
python -m pytest tests/test_noncompress_subagent_history.py -v 2>&1 | tail -30
```
**预期**：8 个测试全失败，错误是 `ImportError: cannot import name '_build_plain_history'` / `'_parse_processed_up_to'`

---

### Task 2: 新增 `_build_plain_history` + `_parse_processed_up_to` helper

**目标**：在 `niu_api/compat.py` 新增两个函数：(1) `_build_plain_history` 构造带 `[N]` 极简前缀的 history 列表 + 返回 idx_to_id 映射；(2) `_parse_processed_up_to` 解析子 Agent 输出中的 `processed_up_to=N`。

- [ ] **Step 2.1**：在 `_build_compress_history` 函数之后（L500 后）新增 `_build_plain_history`

用 Edit 工具在 `niu_api/compat.py` 的 `_build_compress_history` 函数结束（L500 `return history, idx_to_id`）后插入：

old_string:
```python
    return history, idx_to_id


def _strip_analysis(response: str) -> str:
```

new_string:
```python
    return history, idx_to_id


def _build_plain_history(messages, out_msg_ids: list | None = None) -> tuple[list[dict], dict[int, str]]:
    """构造带 [N] 极简前缀的 history 列表 + 简易ID↔UUID 映射（仿 context-manager 的 _build_compress_history）。

    用于非压缩子 Agent（entity-extractor / dream-evolver / journal-agent）的 force/sleep 调用：
    - history 每条 content 前缀 "[N] "（N 是 1-based 简易编号）
    - 同步构建 idx_to_id 映射 {N: 真实UUID}，供程序解析子 Agent 输出的 processed_up_to=N 后查 UUID 更新游标
    - 不排除 PROTECTED 消息（所有消息都该看到）
    - 不排除孤立 tool（保持原顺序，子 Agent 自己判断）

    与 _build_compress_history 的区别：
    - 前缀极简 "[N] "（不是 "[idx:N] Ntokens "）
    - 不排除 PROTECTED / 不排除孤立 tool（调用方按需在调用前过滤 PROTECTED，如 entity force 全量路径，详见 Architecture §6）
    - 不含 token 标注（非压缩子 Agent 不需要做压缩决策）

    Args:
        messages: 全量消息列表（Message 对象，含 id/role/content/tool_calls/tool_call_id）
        out_msg_ids: 输出参数，收集消息的真实 ID 列表（与 history 等长同顺序，用于游标推进兜底）

    Returns:
        (history, idx_to_id):
        - history: [{"role":..., "content": "[N] 原content", "tool_calls"?:..., "tool_call_id"?:...}, ...]
        - idx_to_id: {N: 真实 message_id}，用于解析子 Agent 输出的 processed_up_to=N
    """
    if out_msg_ids is None:
        out_msg_ids = []

    history: list[dict] = []
    idx_to_id: dict[int, str] = {}
    display_idx = 0

    for msg in messages:
        msg_id = getattr(msg, "id", "") or ""
        role = getattr(msg, "role", "user")
        content = getattr(msg, "content", "") or ""
        tool_calls = getattr(msg, "tool_calls", None)
        tool_call_id = getattr(msg, "tool_call_id", None)

        display_idx += 1
        out_msg_ids.append(msg_id)
        idx_to_id[display_idx] = msg_id

        # 极简前缀 [N]（不带 UUID / tokens / role）
        prefix = f"[{display_idx}] "
        entry: dict = {"role": role, "content": prefix + content}
        if tool_calls:
            entry["tool_calls"] = tool_calls
        if tool_call_id:
            entry["tool_call_id"] = tool_call_id

        history.append(entry)

    return history, idx_to_id


def _parse_processed_up_to(response: str) -> int | None:
    """从子 Agent 输出中提取 processed_up_to=N 的 N 值。

    支持格式（大小写不敏感）：
    - "processed_up_to=15"
    - "processed_up_to: 15"
    - "processed_up_to 15"
    - 匹配第一个有效整数

    Args:
        response: 子 Agent 的完整输出文本

    Returns:
        N (int) 或 None（未找到或格式无效）
    """
    import re
    if not response:
        return None
    # 大小写不敏感，支持 = / : / 空格 三种分隔
    # 字符类 [=:\s] 同时匹配 =、: 和纯空格分隔（如 "processed_up_to 15"）
    match = re.search(r'processed_up_to\s*[=:\s]\s*(\d+)', response, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


def _strip_analysis(response: str) -> str:
```

- [ ] **Step 2.2**：跑测试确认 Task 1 的 8 个测试全通过
```bash
cd <repo_root>
python -m pytest tests/test_noncompress_subagent_history.py -v 2>&1 | tail -30
```
**预期**：8 个测试全通过

- [ ] **Step 2.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import niu_api.compat; print('OK')"
```
**预期**：输出 `OK`，无异常

---

### Task 3: 改造 sleep 模式 entity-extractor 调用点

**目标**：把 sleep 模式的 entity-extractor 调用从"task 字符串嵌入消息"改为"history 逐条 + task 独立指令"。

- [ ] **Step 3.1**：定位 `niu_api/compat.py` L1840-1870（sleep 模式 entity 调用点）

当前代码（L1840-1870）：
```python
            # 1/3. entity-extractor（增量，task 方式）
            entity_msg_ids = []
            entity_msg_text = _build_incremental_msg_text(
                messages, last_entity_extract_id, entity_msg_ids, msg_tokens
            )
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_prompt_prefix = """以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

"""
            entity_prompt_suffix = ""
            if entity_msg_ids:
                logger.info(f"[Tidy] entity-extractor: {len(entity_msg_ids)} new messages since cursor")
                entity_full_prompt = entity_prompt_prefix + entity_msg_text + entity_prompt_suffix

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_entity_prompt = _truncate_task_for_subagent(entity_full_prompt, safe_tokens)

                def run_entity_extractor():
                    return call_subagent_with_auto_answer(
                        agent_name="entity-extractor",
                        task=truncated_entity_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=None,
                    )
```

- [ ] **Step 3.2**：用 Edit 工具替换为 history 逐条传版本

old_string:
```python
            # 1/3. entity-extractor（增量，task 方式）
            entity_msg_ids = []
            entity_msg_text = _build_incremental_msg_text(
                messages, last_entity_extract_id, entity_msg_ids, msg_tokens
            )
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_prompt_prefix = """以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

"""
            entity_prompt_suffix = ""
            if entity_msg_ids:
                logger.info(f"[Tidy] entity-extractor: {len(entity_msg_ids)} new messages since cursor")
                entity_full_prompt = entity_prompt_prefix + entity_msg_text + entity_prompt_suffix

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_entity_prompt = _truncate_task_for_subagent(entity_full_prompt, safe_tokens)

                def run_entity_extractor():
                    return call_subagent_with_auto_answer(
                        agent_name="entity-extractor",
                        task=truncated_entity_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=None,
                    )
```

new_string:
```python
            # 1/3. entity-extractor（增量，history 逐条 + task 独立指令）
            entity_msg_ids = []
            # _build_incremental_msg_text 仅用于收集增量范围内的 msg_ids（游标推进用）
            _ = _build_incremental_msg_text(
                messages, last_entity_extract_id, entity_msg_ids, msg_tokens
            )
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_task_prompt = """以下是最近的对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，N 是 1-based 序号）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
            if entity_msg_ids:
                logger.info(f"[Tidy] entity-extractor: {len(entity_msg_ids)} new messages since cursor")
                # 构造增量 history：只含游标之后的消息（按 entity_msg_ids 过滤）
                _id_set = set(entity_msg_ids)
                entity_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                entity_history, entity_idx_to_id = _build_plain_history(entity_incremental_msgs)

                def run_entity_extractor():
                    return call_subagent_with_auto_answer(
                        agent_name="entity-extractor",
                        task=entity_task_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=entity_history,
                        context_fifo_threshold=0,  # 关闭 FIFO，保留完整上下文
                    )
```

同时改造下游的游标推进逻辑（L1877-1884 区域）。当前是：

```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(entity_result):
                    ...
                else:
                    new_entity_id = entity_msg_ids[-1] if entity_msg_ids else last_entity_extract_id
                    logger.info(f"[Tidy] Entity cursor auto-advanced to: {new_entity_id}")
```

改为（新增 `processed_up_to=N` 解析 + 查映射 + `msg_ids[-1]` 兜底）：

old_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动，下次重跑相同范围
                else:
                    new_entity_id = entity_msg_ids[-1] if entity_msg_ids else last_entity_extract_id
                    logger.info(f"[Tidy] Entity cursor auto-advanced to: {new_entity_id}")
```

new_string:
```python
                # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动，下次重跑相同范围
                else:
                    _processed_idx = _parse_processed_up_to(entity_result)
                    if _processed_idx is not None and _processed_idx in entity_idx_to_id:
                        new_entity_id = entity_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Entity cursor advanced per processed_up_to={_processed_idx} -> {new_entity_id}")
                    elif entity_msg_ids:
                        new_entity_id = entity_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Entity cursor fallback to range end: {new_entity_id}")
                    else:
                        new_entity_id = last_entity_extract_id
```

**注意**：`entity_idx_to_id` 是 `_build_plain_history` 返回的元组第二个元素，作用域在 `if entity_msg_ids:` 分支内，游标推进逻辑也在该分支内（L1877 在 `if entity_msg_ids:` 块里），变量可见。

- [ ] **Step 3.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import niu_api.compat; print('OK')"
```
**预期**：输出 `OK`

---

### Task 4: 改造 sleep 模式 dream-evolver 调用点

**目标**：把 sleep 模式的 dream-evolver 调用从"task 字符串嵌入消息"改为"history 逐条 + task 独立指令"。

- [ ] **Step 4.1**：定位 `niu_api/compat.py` L1919-1941（sleep 模式 dream 调用点）

当前代码：
```python
            dream_msg_ids = []
            dream_msg_text = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_msg_ids, msg_tokens
            )
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_msg_text}"""

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_dream_prompt = _truncate_task_for_subagent(dream_prompt, safe_tokens)

                def run_dream_evolver():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=truncated_dream_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )
```

- [ ] **Step 4.2**：用 Edit 工具替换

old_string:
```python
            dream_msg_ids = []
            dream_msg_text = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_msg_ids, msg_tokens
            )
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_msg_text}"""

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_dream_prompt = _truncate_task_for_subagent(dream_prompt, safe_tokens)

                def run_dream_evolver():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=truncated_dream_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )
```

new_string:
```python
            dream_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_msg_ids, msg_tokens
            )
            new_dream_id = last_dream_evolve_id  # 默认保留旧游标
            if dream_msg_ids:
                logger.info(f"[Tidy] dream-evolver: {len(dream_msg_ids)} new messages since cursor")
                dream_task_prompt = """对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造增量 history
                _id_set = set(dream_msg_ids)
                dream_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                dream_history, dream_idx_to_id = _build_plain_history(dream_incremental_msgs)

                def run_dream_evolver():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=dream_task_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=dream_history,
                        context_fifo_threshold=0,
                    )
```

同时改造下游游标推进逻辑（L1950-1957 区域，与 Task 3 同模式）：

old_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动，下次重跑相同范围
                else:
                    new_dream_id = dream_msg_ids[-1] if dream_msg_ids else last_dream_evolve_id
                    logger.info(f"[Tidy] Dream cursor auto-advanced to: {new_dream_id}")
```

new_string:
```python
                # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动，下次重跑相同范围
                else:
                    _processed_idx = _parse_processed_up_to(dream_result)
                    if _processed_idx is not None and _processed_idx in dream_idx_to_id:
                        new_dream_id = dream_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                    elif dream_msg_ids:
                        new_dream_id = dream_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Dream cursor fallback to range end: {new_dream_id}")
                    else:
                        new_dream_id = last_dream_evolve_id
```

- [ ] **Step 4.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import niu_api.compat; print('OK')"
```

---

### Task 5: 改造 sleep 模式 journal-agent 调用点 + `_build_journal_task`

**目标**：把 sleep 模式的 journal-agent 调用从"task 字符串嵌入消息"改为"history 逐条 + task 独立指令"。同时改造 `_build_journal_task` 为纯指令构造器（不再嵌入 msg_text）。

- [ ] **Step 5.1**：改造 `_build_journal_task` 函数（L853-869）

当前代码：
```python
def _build_journal_task(journal_msg_text: str, safe_tokens: int = 0) -> str:
    """构建 journal-agent 的 task prompt（增量消息嵌入）。

    Args:
        journal_msg_text: _build_incremental_msg_text() 返回的增量消息文本
        safe_tokens: 截断 token 上限（0 表示不截断）

    Returns:
        完整的 task prompt 字符串
    """
    prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_msg_text}"""

    if safe_tokens > 0:
        prompt = _truncate_task_for_subagent(prompt, safe_tokens)
    return prompt
```

old_string:
```python
def _build_journal_task(journal_msg_text: str, safe_tokens: int = 0) -> str:
    """构建 journal-agent 的 task prompt（增量消息嵌入）。

    Args:
        journal_msg_text: _build_incremental_msg_text() 返回的增量消息文本
        safe_tokens: 截断 token 上限（0 表示不截断）

    Returns:
        完整的 task prompt 字符串
    """
    prompt = f"""以下是对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

{journal_msg_text}"""

    if safe_tokens > 0:
        prompt = _truncate_task_for_subagent(prompt, safe_tokens)
    return prompt
```

new_string:
```python
def _build_journal_task() -> str:
    """构建 journal-agent 的 task prompt（纯指令，消息以 history 形式逐条传入）。

    Returns:
        纯指令 task prompt 字符串（不含消息文本）
    """
    return """以下是对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，1-based）。请从中识别工作内容，提取为日志条目追加写入 journal.md。

处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
```

- [ ] **Step 5.2**：改造 sleep 模式 journal 调用点（L1994-2012）

当前代码：
```python
                new_journal_id = last_journal_id
                journal_msg_ids = []
                journal_msg_text = _build_incremental_msg_text(
                    messages, last_journal_id, journal_msg_ids, msg_tokens
                )
                logger.info(f"[Tidy] Sleep: starting journal-agent ({len(journal_msg_ids)} incremental messages)")

                if journal_msg_ids:
                    context_window_for_truncate = _read_context_window_tokens()
                    safe_tokens = int(context_window_for_truncate * 0.6)
                    truncated_journal_prompt = _build_journal_task(journal_msg_text, safe_tokens)

                    def run_journal_agent():
                        return call_subagent_with_auto_answer(
                            agent_name="journal-agent",
                            task=truncated_journal_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                        )
```

old_string:
```python
                new_journal_id = last_journal_id
                journal_msg_ids = []
                journal_msg_text = _build_incremental_msg_text(
                    messages, last_journal_id, journal_msg_ids, msg_tokens
                )
                logger.info(f"[Tidy] Sleep: starting journal-agent ({len(journal_msg_ids)} incremental messages)")

                if journal_msg_ids:
                    context_window_for_truncate = _read_context_window_tokens()
                    safe_tokens = int(context_window_for_truncate * 0.6)
                    truncated_journal_prompt = _build_journal_task(journal_msg_text, safe_tokens)

                    def run_journal_agent():
                        return call_subagent_with_auto_answer(
                            agent_name="journal-agent",
                            task=truncated_journal_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                        )
```

new_string:
```python
                new_journal_id = last_journal_id
                journal_msg_ids = []
                _ = _build_incremental_msg_text(
                    messages, last_journal_id, journal_msg_ids, msg_tokens
                )
                logger.info(f"[Tidy] Sleep: starting journal-agent ({len(journal_msg_ids)} incremental messages)")

                if journal_msg_ids:
                    journal_task_prompt = _build_journal_task()
                    # 构造增量 history
                    _id_set = set(journal_msg_ids)
                    journal_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                    journal_history, journal_idx_to_id = _build_plain_history(journal_incremental_msgs)

                    def run_journal_agent():
                        return call_subagent_with_auto_answer(
                            agent_name="journal-agent",
                            task=journal_task_prompt,
                            llm_config=llm_config,
                            mcp_client=None,
                            history=journal_history,
                            context_fifo_threshold=0,
                        )
```

同时改造下游游标推进逻辑（L2021-2028 区域，与 Task 3/4 同模式）：

old_string:
```python
                    # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                    if _is_subagent_overflow(journal_result):
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Tidy] journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow 时游标不动，下次重跑相同范围
                    else:
                        new_journal_id = journal_msg_ids[-1] if journal_msg_ids else last_journal_id
                        logger.info(f"[Tidy] Journal cursor auto-advanced to: {new_journal_id}")
```

new_string:
```python
                    # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                    if _is_subagent_overflow(journal_result):
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Tidy] journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow 时游标不动，下次重跑相同范围
                    else:
                        _processed_idx = _parse_processed_up_to(journal_result)
                        if _processed_idx is not None and _processed_idx in journal_idx_to_id:
                            new_journal_id = journal_idx_to_id[_processed_idx]
                            logger.info(f"[Tidy] Journal cursor advanced per processed_up_to={_processed_idx} -> {new_journal_id}")
                        elif journal_msg_ids:
                            new_journal_id = journal_msg_ids[-1]  # 兜底
                            logger.info(f"[Tidy] Journal cursor fallback to range end: {new_journal_id}")
                        else:
                            new_journal_id = last_journal_id
```

- [ ] **Step 5.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import niu_api.compat; print('OK')"
```

---

### Task 6: 改造 force 模式 entity-extractor 调用点

**目标**：把 force 模式的 entity-extractor 调用从"task 字符串嵌入消息"改为"history 逐条 + task 独立指令"。**同时应用方案 A（排除 PROTECTED 最近 10 条）防止 overflow 死循环**（详见 Architecture §6）。

- [ ] **Step 6.0**：在 force 模式块顶部（L2543 `logger.info("[Tidy] Force mode...")` 之后）补 `_force_protected_ids` 计算

**为什么必须改**：force 模式块当前没有 `protected_ids` 变量（sleep 模式有，force 没有）。方案 A 需要这个变量过滤最近 N 条 PROTECTED 消息（N 由 `_read_protect_recent_count()` 读取，与 context-manager 对齐）。

当前 force 块顶部（L2541-2543）：
```python
        elif mode == "force":
            # Force mode: entity-extractor 全量 → dream-evolver 全量 → context-manager 强制压缩
            logger.info("[Tidy] Force mode: starting entity-extractor (full processing)")
```

old_string:
```python
        elif mode == "force":
            # Force mode: entity-extractor 全量 → dream-evolver 全量 → context-manager 强制压缩
            logger.info("[Tidy] Force mode: starting entity-extractor (full processing)")
```

new_string:
```python
        elif mode == "force":
            # Force mode: entity-extractor 全量 → dream-evolver 全量 → context-manager 强制压缩
            logger.info("[Tidy] Force mode: starting entity-extractor (full processing)")
            # 计算 PROTECTED 消息 ID 集合（最近 N 条 user/assistant，与 context-manager 对齐）
            # 用于 entity force 方案 A：排除最近 PROTECTED 条防止 overflow 死循环（详见 Architecture §6）
            _force_protect_recent_count = _read_protect_recent_count()
            _force_protected_ids: set[str] = set()
            if _force_protect_recent_count > 0 and messages:
                _ua_msgs = [m for m in messages if getattr(m, "role", "") in ("user", "assistant")]
                _force_protected_ids = {getattr(m, "id", "") or "" for m in _ua_msgs[-_force_protect_recent_count:]}
```

**注意**：`_read_protect_recent_count` 已在文件顶部 import（L20），无需额外 import。`messages` 变量在 force 块顶部已存在（L2548 用 `_build_incremental_msg_text(messages, ...)`）。

- [ ] **Step 6.1**：定位 `niu_api/compat.py` L2545-2572（force 模式 entity 调用点）

当前代码：
```python
            # 1/3. entity-extractor（全量 task 方式，cursor 传空 = 全量）
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_force_msg_ids = []
            entity_force_msg_text = _build_incremental_msg_text(
                messages, "", entity_force_msg_ids, msg_tokens
            )
            entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}"""

            # 截断 task 防止子Agent超限
            context_window_for_truncate = _read_context_window_tokens()
            safe_tokens = int(context_window_for_truncate * 0.6)
            truncated_entity_force_prompt = _truncate_task_for_subagent(entity_force_prompt, safe_tokens)

            def run_entity_extractor_force():
                return call_subagent_with_auto_answer(
                    agent_name="entity-extractor",
                    task=truncated_entity_force_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    history=None,
                )
```

- [ ] **Step 6.2**：用 Edit 工具替换

old_string:
```python
            # 1/3. entity-extractor（全量 task 方式，cursor 传空 = 全量）
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_force_msg_ids = []
            entity_force_msg_text = _build_incremental_msg_text(
                messages, "", entity_force_msg_ids, msg_tokens
            )
            entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}"""

            # 截断 task 防止子Agent超限
            context_window_for_truncate = _read_context_window_tokens()
            safe_tokens = int(context_window_for_truncate * 0.6)
            truncated_entity_force_prompt = _truncate_task_for_subagent(entity_force_prompt, safe_tokens)

            def run_entity_extractor_force():
                return call_subagent_with_auto_answer(
                    agent_name="entity-extractor",
                    task=truncated_entity_force_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    history=None,
                )
```

new_string:
```python
            # 1/3. entity-extractor（全量 history 逐条 + task 独立指令，cursor 传空 = 全量）
            new_entity_id = last_entity_extract_id  # 默认保留旧游标
            entity_force_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, "", entity_force_msg_ids, msg_tokens
            )
            entity_force_prompt = """以下是最近的对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，1-based）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
            # 构造全量 history + idx_to_id 映射（force 模式 cursor 为空 = 全量）
            # 方案 A：排除 PROTECTED 消息（最近 N 条 user/assistant）防止 overflow 死循环（详见 Architecture §6）
            # _force_protected_ids 已在 Step 6.0 force 块顶部计算（与 context-manager protect_recent_count 对齐）
            entity_force_msgs_filtered = [m for m in messages if (getattr(m, "id", "") or "") not in _force_protected_ids]
            entity_force_history, entity_force_idx_to_id = _build_plain_history(entity_force_msgs_filtered)
            # 同步过滤 entity_force_msg_ids（游标推进兜底用，与 history 保持一致）
            entity_force_msg_ids = [getattr(m, "id", "") or "" for m in entity_force_msgs_filtered]

            def run_entity_extractor_force():
                return call_subagent_with_auto_answer(
                    agent_name="entity-extractor",
                    task=entity_force_prompt,
                    llm_config=llm_config,
                    mcp_client=None,
                    history=entity_force_history,
                    context_fifo_threshold=0,
                )
```

**注意**：
- `_force_protected_ids` 变量已在 Step 6.0 force 块顶部计算（最近 N 条 user/assistant 消息 ID），此处直接复用
- entity force 的 `entity_force_msg_ids` 同步过滤，保证游标推进兜底（`msg_ids[-1]`）指向过滤后列表的末尾，而非全量列表的末尾（否则游标会跳过被排除的 PROTECTED 消息）

**Why must filter entity_force_msg_ids too**：`_build_incremental_msg_text(messages, "", entity_force_msg_ids, ...)` 收集的是全量 600 条 ID，但 history 只含 590 条（过滤后）。如果游标推进走 `msg_ids[-1]` 兜底，会推进到全量末尾（第 600 条 UUID），但子 Agent 实际只处理到第 590 条——游标跳过了 10 条 PROTECTED。过滤后两者一致，游标推进正确。

同时改造下游游标推进逻辑（L2580-2586 区域）：

old_string:
```python
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] Force: entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动
                else:
                    new_entity_id = entity_force_msg_ids[-1] if entity_force_msg_ids else last_entity_extract_id
                    logger.info(f"[Tidy] Force: Entity cursor auto-advanced to: {new_entity_id}")
```

new_string:
```python
                if _is_subagent_overflow(entity_result):
                    overflow_info = _extract_overflow_info(entity_result)
                    logger.warning(f"[Tidy] Force: entity-extractor overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动
                else:
                    _processed_idx = _parse_processed_up_to(entity_result)
                    if _processed_idx is not None and _processed_idx in entity_force_idx_to_id:
                        new_entity_id = entity_force_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Force: Entity cursor advanced per processed_up_to={_processed_idx} -> {new_entity_id}")
                    elif entity_force_msg_ids:
                        new_entity_id = entity_force_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Force: Entity cursor fallback to range end: {new_entity_id}")
                    else:
                        new_entity_id = last_entity_extract_id
```

- [ ] **Step 6.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import niu_api.compat; print('OK')"
```

---

### Task 7: 改造 force 模式 dream-evolver 调用点

**目标**：把 force 模式的 dream-evolver 调用从"task 字符串嵌入消息"改为"history 逐条 + task 独立指令"。

- [ ] **Step 7.1**：定位 `niu_api/compat.py` L2619-2641（force 模式 dream 调用点）

当前代码：
```python
            dream_force_msg_ids = []
            dream_force_msg_text = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force mode: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}"""

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_dream_force_prompt = _truncate_task_for_subagent(dream_force_prompt, safe_tokens)

                def run_dream_evolver_force():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=truncated_dream_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )
```

- [ ] **Step 7.2**：用 Edit 工具替换

old_string:
```python
            dream_force_msg_ids = []
            dream_force_msg_text = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force mode: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}"""

                # 截断 task 防止子Agent超限
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_dream_force_prompt = _truncate_task_for_subagent(dream_force_prompt, safe_tokens)

                def run_dream_evolver_force():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=truncated_dream_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )
```

new_string:
```python
            dream_force_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force mode: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = """对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造增量 history
                _id_set = set(dream_force_msg_ids)
                dream_force_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                dream_force_history, dream_force_idx_to_id = _build_plain_history(dream_force_incremental_msgs)

                def run_dream_evolver_force():
                    return call_subagent_with_auto_answer(
                        agent_name="dream-evolver",
                        task=dream_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=dream_force_history,
                        context_fifo_threshold=0,
                    )
```

同时改造下游游标推进逻辑（L2650-2657 区域）：

old_string:
```python
                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动
                else:
                    new_dream_id = dream_force_msg_ids[-1] if dream_force_msg_ids else last_dream_evolve_id
                    logger.info(f"[Tidy] Force: Dream cursor auto-advanced to: {new_dream_id}")
```

new_string:
```python
                # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(dream_result):
                    overflow_info = _extract_overflow_info(dream_result)
                    logger.warning(f"[Tidy] Force: Dream-evolver overflow: {overflow_info.get('turns_completed', 0)} turns, {overflow_info.get('tokens_used', 0)} tokens")
                    # overflow 时游标不动
                else:
                    _processed_idx = _parse_processed_up_to(dream_result)
                    if _processed_idx is not None and _processed_idx in dream_force_idx_to_id:
                        new_dream_id = dream_force_idx_to_id[_processed_idx]
                        logger.info(f"[Tidy] Force: Dream cursor advanced per processed_up_to={_processed_idx} -> {new_dream_id}")
                    elif dream_force_msg_ids:
                        new_dream_id = dream_force_msg_ids[-1]  # 兜底
                        logger.info(f"[Tidy] Force: Dream cursor fallback to range end: {new_dream_id}")
                    else:
                        new_dream_id = last_dream_evolve_id
```

- [ ] **Step 7.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import niu_api.compat; print('OK')"
```

---

### Task 8: 改造 force 模式 journal-agent 调用点

**目标**：把 force 模式的 journal-agent 调用从"task 字符串嵌入消息"改为"history 逐条 + task 独立指令"。

- [ ] **Step 8.1**：定位 `niu_api/compat.py` L2694-2712（force 模式 journal 调用点）

当前代码：
```python
            new_journal_id = last_journal_id
            journal_force_msg_ids = []
            journal_force_msg_text = _build_incremental_msg_text(
                messages, last_journal_id, journal_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force: starting journal-agent ({len(journal_force_msg_ids)} incremental messages)")

            if journal_force_msg_ids:
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_journal_force_prompt = _build_journal_task(journal_force_msg_text, safe_tokens)

                def run_journal_agent_force():
                    return call_subagent_with_auto_answer(
                        agent_name="journal-agent",
                        task=truncated_journal_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )
```

- [ ] **Step 8.2**：用 Edit 工具替换

old_string:
```python
            new_journal_id = last_journal_id
            journal_force_msg_ids = []
            journal_force_msg_text = _build_incremental_msg_text(
                messages, last_journal_id, journal_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force: starting journal-agent ({len(journal_force_msg_ids)} incremental messages)")

            if journal_force_msg_ids:
                context_window_for_truncate = _read_context_window_tokens()
                safe_tokens = int(context_window_for_truncate * 0.6)
                truncated_journal_force_prompt = _build_journal_task(journal_force_msg_text, safe_tokens)

                def run_journal_agent_force():
                    return call_subagent_with_auto_answer(
                        agent_name="journal-agent",
                        task=truncated_journal_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                    )
```

new_string:
```python
            new_journal_id = last_journal_id
            journal_force_msg_ids = []
            _ = _build_incremental_msg_text(
                messages, last_journal_id, journal_force_msg_ids, msg_tokens
            )
            logger.info(f"[Tidy] Force: starting journal-agent ({len(journal_force_msg_ids)} incremental messages)")

            if journal_force_msg_ids:
                journal_force_prompt = _build_journal_task()  # 纯指令，无参（含 processed_up_to 说明）
                # 构造增量 history
                _id_set = set(journal_force_msg_ids)
                journal_force_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
                journal_force_history, journal_force_idx_to_id = _build_plain_history(journal_force_incremental_msgs)

                def run_journal_agent_force():
                    return call_subagent_with_auto_answer(
                        agent_name="journal-agent",
                        task=journal_force_prompt,
                        llm_config=llm_config,
                        mcp_client=None,
                        history=journal_force_history,
                        context_fifo_threshold=0,
                    )
```

同时改造下游游标推进逻辑（L2722-2728 区域）：

old_string:
```python
                    # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                    if _is_subagent_overflow(journal_result):
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Tidy] Force: journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow 时游标不动
                    else:
                        new_journal_id = journal_force_msg_ids[-1] if journal_force_msg_ids else last_journal_id
                        logger.info(f"[Tidy] Force: Journal cursor auto-advanced to: {new_journal_id}")
```

new_string:
```python
                    # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                    if _is_subagent_overflow(journal_result):
                        overflow_info = _extract_overflow_info(journal_result)
                        logger.warning(f"[Tidy] Force: journal-agent overflow: {overflow_info.get('turns_completed', 0)} turns")
                        # overflow 时游标不动
                    else:
                        _processed_idx = _parse_processed_up_to(journal_result)
                        if _processed_idx is not None and _processed_idx in journal_force_idx_to_id:
                            new_journal_id = journal_force_idx_to_id[_processed_idx]
                            logger.info(f"[Tidy] Force: Journal cursor advanced per processed_up_to={_processed_idx} -> {new_journal_id}")
                        elif journal_force_msg_ids:
                            new_journal_id = journal_force_msg_ids[-1]  # 兜底
                            logger.info(f"[Tidy] Force: Journal cursor fallback to range end: {new_journal_id}")
                        else:
                            new_journal_id = last_journal_id
```

- [ ] **Step 8.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import niu_api.compat; print('OK')"
```

- [ ] **Step 8.4**：跑 Task 1 的测试确认仍通过
```bash
cd <repo_root>
python -m pytest tests/test_noncompress_subagent_history.py -v 2>&1 | tail -20
```

---

### Task 9: 改造 `agent/handler.py` 主 Agent 触发 journal-agent 路径

**目标**：把 `agent/handler.py:824-882` 的 `_build_journal_task_for_handler` 从"返回 `(task, journal_msg_ids)`，task 嵌入消息文本"改为"返回 `(task, history, idx_to_id, journal_msg_ids)` 四元组，task 纯指令 + history 逐条 + idx_to_id 映射"。同时改 `agent/handler.py:884-937` 的 `_update_journal_cursor` 用 `_parse_processed_up_to` + 查映射更新游标（带兜底），以及 `agent/handler.py:939-1010` 的 `_call_subagent_gen` 接收 history + idx_to_id 并透传给 `call_subagent` / `_update_journal_cursor`。

**为什么必须改**：Task 5.1 把 `_build_journal_task` 签名从 `(journal_msg_text, safe_tokens=0)` 改为 `()`，但 `handler.py:882` 还在调用 `_build_journal_task(journal_msg_text, safe_tokens)`，改造后会 `TypeError`，主 Agent 调用 journal-agent 全部崩溃。

- [ ] **Step 9.1**：改造 `_build_journal_task_for_handler`（`agent/handler.py:824-882`）

当前代码有三个返回路径（L824-882 关键段）：
- L833 报告生成场景：`return original_task, []`（二元组）
- L848 无消息场景：`return original_task, []`（二元组）
- L876-882 主返回路径：`return _build_journal_task(journal_msg_text, safe_tokens), journal_msg_ids`（二元组）

**三个返回路径必须全部改为四元组**（`return task, history, idx_to_id, msg_ids`），否则 Task 9.2 的 `_call_subagent_gen` 解构四元组 `task, _journal_history, _journal_idx_to_id, journal_msg_ids_for_cursor = self._build_journal_task_for_handler(task)` 会 `ValueError: too many values to unpack (expected 4, got 2)`，主 Agent 调用 `chat-with-journal-agent` 触发报告生成或无消息场景会崩溃。

当前代码（L824-882 关键段）：
```python
    def _build_journal_task_for_handler(self, original_task: str) -> tuple:
        """为主Agent调用 journal-agent 构建增量消息 task。"""
        import json
        from niu_api.compat import _build_journal_task, _build_incremental_msg_text
        from agent.subagent import _read_context_window_tokens

        # 报告生成指令不替换为增量消息 task — journal-agent 自己读 journal.md 聚合
        report_keywords = ("周报", "月报", "季报", "年报")
        if any(kw in original_task for kw in report_keywords):
            return original_task, []

        # ... 读游标 + 获取消息 + 计算 msg_tokens + 构建 journal_msg_text/ids ...

        # 5. 构建增量消息文本
        journal_msg_ids = []
        journal_msg_text = _build_incremental_msg_text(
            messages, last_journal_id, journal_msg_ids, msg_tokens
        )

        if not journal_msg_ids:
            return original_task, []

        # 6. 构建完整 task
        context_window_for_truncate = _read_context_window_tokens()
        safe_tokens = int(context_window_for_truncate * 0.6)
        return _build_journal_task(journal_msg_text, safe_tokens), journal_msg_ids
```

用 Edit 工具改造（关键改动：返回值从二元组改四元组 + 用 `_build_plain_history` 构造 history + idx_to_id + 调 `_build_journal_task()` 无参）：

old_string（L827 + L830-833 报告生成早返回 + L870-882 末尾段，需分别 Edit）：

第一处 Edit（import 行 L827）：
```python
        from niu_api.compat import _build_journal_task, _build_incremental_msg_text
```
new_string:
```python
        from niu_api.compat import _build_journal_task, _build_incremental_msg_text, _build_plain_history
```

第二处 Edit（L830-833 报告生成早返回路径 — 二元组改四元组）：
```python
        # 报告生成指令不替换为增量消息 task — journal-agent 自己读 journal.md 聚合
        report_keywords = ("周报", "月报", "季报", "年报")
        if any(kw in original_task for kw in report_keywords):
            return original_task, []
```
new_string:
```python
        # 报告生成指令不替换为增量消息 task — journal-agent 自己读 journal.md 聚合
        # 返回四元组 (task, history=[], idx_to_id={}, msg_ids=[])，与主返回路径结构一致
        report_keywords = ("周报", "月报", "季报", "年报")
        if any(kw in original_task for kw in report_keywords):
            return original_task, [], {}, []
```

第三处 Edit（L870-882 末尾段 — 含 L877 无消息早返回 + L882 主返回路径）：
```python
        # 5. 构建增量消息文本
        journal_msg_ids = []
        journal_msg_text = _build_incremental_msg_text(
            messages, last_journal_id, journal_msg_ids, msg_tokens
        )

        if not journal_msg_ids:
            return original_task, []

        # 6. 构建完整 task
        context_window_for_truncate = _read_context_window_tokens()
        safe_tokens = int(context_window_for_truncate * 0.6)
        return _build_journal_task(journal_msg_text, safe_tokens), journal_msg_ids
```
new_string:
```python
        # 5. 收集增量消息 ID（不再用 journal_msg_text 构造 task）
        journal_msg_ids = []
        _ = _build_incremental_msg_text(
            messages, last_journal_id, journal_msg_ids, msg_tokens
        )

        # 无增量消息早返回：四元组（history 空 + idx_to_id 空字典 + msg_ids 空列表）
        if not journal_msg_ids:
            return original_task, [], {}, []

        # 6. 构造增量 history + idx_to_id 映射（按 journal_msg_ids 过滤，保留双游标区间内的消息）
        _id_set = set(journal_msg_ids)
        journal_incremental_msgs = [m for m in messages if (getattr(m, "id", "") or "") in _id_set]
        journal_history, journal_idx_to_id = _build_plain_history(journal_incremental_msgs)

        # 7. 返回 (task 纯指令, history 逐条, idx_to_id 映射, journal_msg_ids)
        return _build_journal_task(), journal_history, journal_idx_to_id, journal_msg_ids
```

**验收**：三个返回路径全部返回四元组：
- L833 报告生成场景：`return original_task, [], {}, []` ✅
- L848 无消息场景：`return original_task, [], {}, []` ✅
- L876-882 主返回路径：`return _build_journal_task(), journal_history, journal_idx_to_id, journal_msg_ids` ✅

- [ ] **Step 9.1.1**：改造 `_update_journal_cursor`（`agent/handler.py:884-937`）用 `processed_up_to=N` + 查映射

当前代码（L911-921 关键段）：
```python
                new_journal_id = last_journal_id

                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    logger.warning(f"[Journal] overflow: {overflow_info.get('turns_completed', 0)} turns")
                    # overflow 时游标不动
                    new_journal_id = last_journal_id
                else:
                    new_journal_id = journal_msg_ids[-1] if journal_msg_ids else last_journal_id
                    logger.info(f"[Journal] Cursor auto-advanced to: {new_journal_id}")
```

需扩展 `_update_journal_cursor` 签名新增 `journal_idx_to_id: dict` 参数，并改造游标推进逻辑。

第一处 Edit（函数签名 L884）：
```python
    def _update_journal_cursor(self, journal_result: str, journal_msg_ids: list):
        """从 journal-agent 结果中提取游标并更新 last_journal.json"""
```
new_string:
```python
    def _update_journal_cursor(self, journal_result: str, journal_msg_ids: list, journal_idx_to_id: dict | None = None):
        """从 journal-agent 结果中提取游标并更新 last_journal.json

        仿 context-manager 简易 ID 映射：解析子 Agent 输出的 processed_up_to=N，
        查 journal_idx_to_id[N] 得到真实 UUID 更新游标；未找到则回退到 msg_ids[-1]（兜底）。
        """
```

第二处 Edit（import 行 L889）：
```python
        from niu_api.compat import _is_subagent_overflow, _extract_overflow_info
```
new_string:
```python
        from niu_api.compat import _is_subagent_overflow, _extract_overflow_info, _parse_processed_up_to
```

第三处 Edit（L911-921 游标推进段）：
```python
                new_journal_id = last_journal_id

                # 游标自动推进：成功→推进到增量范围末尾，overflow→不动
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    logger.warning(f"[Journal] overflow: {overflow_info.get('turns_completed', 0)} turns")
                    # overflow 时游标不动
                    new_journal_id = last_journal_id
                else:
                    new_journal_id = journal_msg_ids[-1] if journal_msg_ids else last_journal_id
                    logger.info(f"[Journal] Cursor auto-advanced to: {new_journal_id}")
```
new_string:
```python
                new_journal_id = last_journal_id

                # 游标推进：overflow→不动；否则解析 processed_up_to=N 查映射，兜底 msg_ids[-1]
                if _is_subagent_overflow(journal_result):
                    overflow_info = _extract_overflow_info(journal_result)
                    logger.warning(f"[Journal] overflow: {overflow_info.get('turns_completed', 0)} turns")
                    # overflow 时游标不动
                    new_journal_id = last_journal_id
                else:
                    _processed_idx = _parse_processed_up_to(journal_result)
                    if _processed_idx is not None and journal_idx_to_id and _processed_idx in journal_idx_to_id:
                        new_journal_id = journal_idx_to_id[_processed_idx]
                        logger.info(f"[Journal] Cursor advanced per processed_up_to={_processed_idx} -> {new_journal_id}")
                    elif journal_msg_ids:
                        new_journal_id = journal_msg_ids[-1]  # 兜底
                        logger.info(f"[Journal] Cursor fallback to range end: {new_journal_id}")
                    else:
                        new_journal_id = last_journal_id
```

- [ ] **Step 9.2**：改造 `_call_subagent_gen` 接收 history + idx_to_id 并透传（`agent/handler.py:939-1010`）

当前代码（L948-951）：
```python
        # journal-agent 特殊处理：构建增量消息 task，与 tidy 管道一致
        journal_msg_ids_for_cursor = []  # 默认空列表，仅 journal-agent 时填充
        if agent_name == "journal-agent":
            task, journal_msg_ids_for_cursor = self._build_journal_task_for_handler(task)
```

old_string:
```python
        # journal-agent 特殊处理：构建增量消息 task，与 tidy 管道一致
        journal_msg_ids_for_cursor = []  # 默认空列表，仅 journal-agent 时填充
        if agent_name == "journal-agent":
            task, journal_msg_ids_for_cursor = self._build_journal_task_for_handler(task)
```

new_string:
```python
        # journal-agent 特殊处理：构建增量消息 task + history + idx_to_id，与 tidy 管道一致
        journal_msg_ids_for_cursor = []  # 默认空列表，仅 journal-agent 时填充
        _journal_history = []  # 默认空 history，仅 journal-agent 时填充
        _journal_idx_to_id = {}  # 默认空映射，仅 journal-agent 时填充
        if agent_name == "journal-agent":
            task, _journal_history, _journal_idx_to_id, journal_msg_ids_for_cursor = self._build_journal_task_for_handler(task)
```

然后在 `call_subagent(...)` 调用处（L1000 附近，透传 history）：

定位 `result = call_subagent(` 那段（L1000-1010），找到 `history=_history,` 那行，确认 `_history` 的赋值来源。如果 `_history` 是从 `args.get("history")` 取的，需在 journal-agent 分支覆盖：

old_string（需先 Read 确认 L995-1010 的实际代码，假设是）：
```python
            _history = args.get("history") or []
            ...
            result = call_subagent(
                ...
                history=_history,
                ...
            )
```

new_string:
```python
            _history = args.get("history") or []
            # journal-agent 的 history 来自 _build_journal_task_for_handler，覆盖 args 的 history
            if agent_name == "journal-agent" and _journal_history:
                _history = _journal_history
            ...
            result = call_subagent(
                ...
                history=_history,
                ...
            )
```

**同时**：`_call_subagent_gen` 在拿到 `result` 后会调 `_update_journal_cursor`（Step 9.1.1 改造后新增 `journal_idx_to_id` 参数），需透传 `_journal_idx_to_id`。找到调用 `_update_journal_cursor` 的地方（应在 `result = call_subagent(...)` 之后），把 `_journal_idx_to_id` 传进去：

old_string（需先 Read 确认实际代码，假设是）：
```python
            if agent_name == "journal-agent" and journal_msg_ids_for_cursor:
                self._update_journal_cursor(result, journal_msg_ids_for_cursor)
```

new_string:
```python
            if agent_name == "journal-agent" and journal_msg_ids_for_cursor:
                self._update_journal_cursor(result, journal_msg_ids_for_cursor, _journal_idx_to_id)
```

**注意**：Step 9.2 的具体 old_string/new_string 需要执行子 Agent 先 Read `agent/handler.py:995-1010` 确认实际代码后再 Edit，本计划只给方向。

- [ ] **Step 9.3**：Python 语法检查
```bash
cd <repo_root>
python -c "import agent.handler; print('OK')"
```
**预期**：输出 `OK`

---

### Task 10: 改造 `agent/runner.py` force 镜像路径（同步版 3 个调用点 + 扩展 `_run_subagent_step`）

**目标**：`agent/runner.py` 的 `_on_context_high_usage`（L1022 起）是 `compat.py` force 模式的同步镜像，包含 entity/dream/journal 三个调用点 + `_run_subagent_step` 封装。它们也用"task 字符串嵌入消息"模式，必须同步改造。

**为什么必须改**：runner.py 的 force 镜像路径同样会把 600 条消息拼成 task 字符串，触发同样的截断 bug + 指令内容混合风险。Task 5.1 改了 `_build_journal_task` 签名后，`runner.py:1187` 调用 `_build_journal_task(journal_force_msg_text, safe_tokens)` 也会 `TypeError`。

- [ ] **Step 10.1**：扩展 `_run_subagent_step` 签名 + 改造内部游标推进（`agent/runner.py:942-1020`）

当前签名（L942-944）：
```python
    def _run_subagent_step(self, step_name, cursor_path, cursor_field,
                           prompt, llm_config, last_cursor_id,
                           fallback_ids, timestamp_field):
```

当前内部调用（L980）：
```python
            future = executor.submit(call_subagent_with_auto_answer, step_name, prompt, llm_config=llm_config, mcp_client=None)
```

当前游标推进（L989-998）：
```python
        # --- cursor auto-advance: success→advance to end of incremental range, overflow→don't move ---
        new_cursor_id = last_cursor_id
        if _is_subagent_overflow(result):
            overflow_info = _extract_overflow_info(result)
            logger.warning(f"[{step_name}] overflow: {overflow_info.get('turns_completed', 0)} turns")
            # overflow 时游标不动
            new_cursor_id = last_cursor_id
        else:
            new_cursor_id = fallback_ids[-1] if fallback_ids else last_cursor_id
            logger.info(f"[{step_name}] Cursor auto-advanced to: {new_cursor_id}")
```

old_string（L942-944 签名）:
```python
    def _run_subagent_step(self, step_name, cursor_path, cursor_field,
                           prompt, llm_config, last_cursor_id,
                           fallback_ids, timestamp_field):
```

new_string:
```python
    def _run_subagent_step(self, step_name, cursor_path, cursor_field,
                           prompt, llm_config, last_cursor_id,
                           fallback_ids, timestamp_field,
                           history=None, context_fifo_threshold=None,
                           idx_to_id=None):
```

old_string（L980 内部调用）:
```python
            future = executor.submit(call_subagent_with_auto_answer, step_name, prompt, llm_config=llm_config, mcp_client=None)
```

new_string:
```python
            _kwargs = dict(llm_config=llm_config, mcp_client=None)
            if history is not None:
                _kwargs["history"] = history
            if context_fifo_threshold is not None:
                _kwargs["context_fifo_threshold"] = context_fifo_threshold
            future = executor.submit(call_subagent_with_auto_answer, step_name, prompt, **_kwargs)
```

old_string（L989-998 游标推进）:
```python
        # --- cursor auto-advance: success→advance to end of incremental range, overflow→don't move ---
        new_cursor_id = last_cursor_id
        if _is_subagent_overflow(result):
            overflow_info = _extract_overflow_info(result)
            logger.warning(f"[{step_name}] overflow: {overflow_info.get('turns_completed', 0)} turns")
            # overflow 时游标不动
            new_cursor_id = last_cursor_id
        else:
            new_cursor_id = fallback_ids[-1] if fallback_ids else last_cursor_id
            logger.info(f"[{step_name}] Cursor auto-advanced to: {new_cursor_id}")
```

new_string:
```python
        # --- cursor advance: overflow→don't move; else parse processed_up_to=N + lookup idx_to_id, fallback fallback_ids[-1] ---
        new_cursor_id = last_cursor_id
        if _is_subagent_overflow(result):
            overflow_info = _extract_overflow_info(result)
            logger.warning(f"[{step_name}] overflow: {overflow_info.get('turns_completed', 0)} turns")
            # overflow 时游标不动
            new_cursor_id = last_cursor_id
        else:
            from niu_api.compat import _parse_processed_up_to
            _processed_idx = _parse_processed_up_to(result)
            if _processed_idx is not None and idx_to_id and _processed_idx in idx_to_id:
                new_cursor_id = idx_to_id[_processed_idx]
                logger.info(f"[{step_name}] Cursor advanced per processed_up_to={_processed_idx} -> {new_cursor_id}")
            elif fallback_ids:
                new_cursor_id = fallback_ids[-1]  # 兜底
                logger.info(f"[{step_name}] Cursor fallback to range end: {new_cursor_id}")
            else:
                new_cursor_id = last_cursor_id
```

- [ ] **Step 10.2**：改造 force 镜像 entity 调用点（`agent/runner.py:1108-1126`）

当前代码（L1108-1126）：
```python
            entity_force_msg_ids = []
            entity_force_msg_text = _build_incremental_msg_text(
                db_messages, "", entity_force_msg_ids, msg_tokens
            )
            if entity_force_msg_ids:
                entity_force_prompt = f"""以下是最近的对话消息（每条带 [id:UUID] [idx:N] 序号标注）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

{entity_force_msg_text}"""

                safe_tokens = int(_read_context_window_tokens() * 0.6)
                truncated_entity_prompt = _truncate_task_for_subagent(entity_force_prompt, safe_tokens)

                _, new_entity_id = self._run_subagent_step(
                    "entity-extractor", entity_cursor_path, "last_entity_extract_id",
                    truncated_entity_prompt, llm_config, last_entity_extract_id,
                    entity_force_msg_ids, "last_entity_extract_at",
                )
```

**改造前先补 `_force_protected_ids` 计算**（runner force 块顶部，L1101 `logger.info("[Runner] Force: starting entity-extractor...")` 之后，`new_entity_id = last_entity_extract_id` 之前或之后均可，只要在 `if entity_force_msg_ids:` 块之前）：

old_string:
```python
            # === 步骤 1/4: entity-extractor（全量，cursor 传空 = 全量）===
            logger.info("[Runner] Force: starting entity-extractor (full processing)")
            new_entity_id = last_entity_extract_id
```

new_string:
```python
            # === 步骤 1/4: entity-extractor（全量，cursor 传空 = 全量）===
            logger.info("[Runner] Force: starting entity-extractor (full processing)")
            new_entity_id = last_entity_extract_id
            # 计算 PROTECTED 消息 ID 集合（最近 N 条 user/assistant，与 context-manager 对齐）
            # 用于 entity force 方案 A：排除最近 PROTECTED 条防止 overflow 死循环（详见 Architecture §6）
            _force_protect_recent_count = _read_protect_recent_count()
            _force_protected_ids: set[str] = set()
            if _force_protect_recent_count > 0 and db_messages:
                _ua_msgs = [m for m in db_messages if getattr(m, "role", "") in ("user", "assistant")]
                _force_protected_ids = {getattr(m, "id", "") or "" for m in _ua_msgs[-_force_protect_recent_count:]}
```

**注意**：`_read_protect_recent_count` 已在 L1047 import，无需额外 import。`db_messages` 是 runner force 块顶部已存在的消息列表变量。

new_string（entity 调用点本体）：
```python
            entity_force_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, "", entity_force_msg_ids, msg_tokens
            )
            if entity_force_msg_ids:
                entity_force_prompt = """以下是最近的对话消息（以 history 形式逐条传入，每条 content 前缀 [N] 极简编号，1-based）。请从中提取有价值的内容，形成精炼文档提交给 LightRAG 入库。

注意：对话历史中包含工具调用结果（role=tool），这些是程序化操作的结果。照片入库、人物命名等操作已经自动完成了知识图谱写入，不要重复创建这些实体。如果需要关联已有实体，请使用入库后的实体名称。

处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造全量 history + idx_to_id 映射（force 模式 cursor 为空 = 全量）
                # 方案 A：排除 PROTECTED 消息（最近 N 条 user/assistant）防止 overflow 死循环（详见 Architecture §6）
                entity_force_msgs_filtered = [m for m in db_messages if (getattr(m, "id", "") or "") not in _force_protected_ids]
                entity_force_history, entity_force_idx_to_id = _build_plain_history(entity_force_msgs_filtered)
                # 同步过滤 entity_force_msg_ids（游标推进兜底用，与 history 保持一致）
                entity_force_msg_ids = [getattr(m, "id", "") or "" for m in entity_force_msgs_filtered]

                _, new_entity_id = self._run_subagent_step(
                    "entity-extractor", entity_cursor_path, "last_entity_extract_id",
                    entity_force_prompt, llm_config, last_entity_extract_id,
                    entity_force_msg_ids, "last_entity_extract_at",
                    history=entity_force_history, context_fifo_threshold=0,
                    idx_to_id=entity_force_idx_to_id,
                )
```

**注意**：
- 需在 `_on_context_high_usage` 顶部的 `from niu_api.compat import (...)` 里加 `_build_plain_history`（Step 10.5 已覆盖）
- entity force 的 `entity_force_msg_ids` 同步过滤，保证游标推进兜底（`fallback_ids[-1]`）指向过滤后列表的末尾（与 compat Task 6.2 同理）

- [ ] **Step 10.3**：改造 force 镜像 dream 调用点（`agent/runner.py:1144-1162`）

当前代码（L1144-1162）：
```python
            new_dream_id = last_dream_evolve_id
            dream_force_msg_ids = []
            dream_force_msg_text = _build_incremental_msg_text(
                db_messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Runner] Force: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = f"""对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

{dream_force_msg_text}"""

                safe_tokens = int(_read_context_window_tokens() * 0.6)
                truncated_dream_prompt = _truncate_task_for_subagent(dream_force_prompt, safe_tokens)

                _, new_dream_id = self._run_subagent_step(
                    "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                    truncated_dream_prompt, llm_config, last_dream_evolve_id,
                    dream_force_msg_ids, "last_evolve_at",
                )
```

new_string:
```python
            new_dream_id = last_dream_evolve_id
            dream_force_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_dream_evolve_id, dream_force_msg_ids, msg_tokens
            )
            logger.info(f"[Runner] Force: starting dream-evolver ({len(dream_force_msg_ids)} incremental messages)")

            if dream_force_msg_ids:
                dream_force_prompt = """对以下消息中涉及的实体进行精加工（打标签、建关系、关联脑区、更新画像），并维护 skill 文件。

消息以 history 形式逐条传入，每条 content 前缀 [N] 极简编号（1-based）。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标。如果未输出该行，程序会回退到区间末尾作为游标（兜底）。"""
                # 构造增量 history + idx_to_id 映射
                _id_set = set(dream_force_msg_ids)
                dream_force_incremental_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
                dream_force_history, dream_force_idx_to_id = _build_plain_history(dream_force_incremental_msgs)

                _, new_dream_id = self._run_subagent_step(
                    "dream-evolver", dream_cursor_path, "last_dream_evolve_id",
                    dream_force_prompt, llm_config, last_dream_evolve_id,
                    dream_force_msg_ids, "last_evolve_at",
                    history=dream_force_history, context_fifo_threshold=0,
                    idx_to_id=dream_force_idx_to_id,
                )
```

- [ ] **Step 10.4**：改造 force 镜像 journal 调用点（`agent/runner.py:1178-1193`）

当前代码（L1178-1193）：
```python
            new_journal_id = last_journal_id
            journal_force_msg_ids = []
            journal_force_msg_text = _build_incremental_msg_text(
                db_messages, last_journal_id, journal_force_msg_ids, msg_tokens
            )
            logger.info(f"[Runner] Force: starting journal-agent ({len(journal_force_msg_ids)} incremental messages)")

            if journal_force_msg_ids:
                safe_tokens = int(_read_context_window_tokens() * 0.6)
                truncated_journal_prompt = _build_journal_task(journal_force_msg_text, safe_tokens)

                _, new_journal_id = self._run_subagent_step(
                    "journal-agent", journal_cursor_path, "last_journal_id",
                    truncated_journal_prompt, llm_config, last_journal_id,
                    journal_force_msg_ids, "last_journal_at",
                )
```

new_string:
```python
            new_journal_id = last_journal_id
            journal_force_msg_ids = []
            _ = _build_incremental_msg_text(
                db_messages, last_journal_id, journal_force_msg_ids, msg_tokens
            )
            logger.info(f"[Runner] Force: starting journal-agent ({len(journal_force_msg_ids)} incremental messages)")

            if journal_force_msg_ids:
                journal_force_prompt = _build_journal_task()  # 纯指令，无参（含 processed_up_to 说明）
                # 构造增量 history + idx_to_id 映射
                _id_set = set(journal_force_msg_ids)
                journal_force_incremental_msgs = [m for m in db_messages if (getattr(m, "id", "") or "") in _id_set]
                journal_force_history, journal_force_idx_to_id = _build_plain_history(journal_force_incremental_msgs)

                _, new_journal_id = self._run_subagent_step(
                    "journal-agent", journal_cursor_path, "last_journal_id",
                    journal_force_prompt, llm_config, last_journal_id,
                    journal_force_msg_ids, "last_journal_at",
                    history=journal_force_history, context_fifo_threshold=0,
                    idx_to_id=journal_force_idx_to_id,
                )
```

- [ ] **Step 10.5**：补 `_build_plain_history` 到 import 列表

在 `_on_context_high_usage` 顶部的 `from niu_api.compat import (...)`（L1033-1042）加一行：

old_string:
```python
        from niu_api.compat import (
            _build_incremental_msg_text,
            _truncate_task_for_subagent,
            _build_journal_task,
            _write_cursor_with_lock,
            _parse_idx_list,
            _build_force_prompt,
            _strip_analysis,
            _build_compress_history,
        )
```

new_string:
```python
        from niu_api.compat import (
            _build_incremental_msg_text,
            _truncate_task_for_subagent,
            _build_journal_task,
            _build_plain_history,
            _write_cursor_with_lock,
            _parse_idx_list,
            _build_force_prompt,
            _strip_analysis,
            _build_compress_history,
        )
```

- [ ] **Step 10.6**：Python 语法检查
```bash
cd <repo_root>
python -c "import agent.runner; print('OK')"
```
**预期**：输出 `OK`

---

### Task 11: 同步修改三个子 Agent 的 system prompt

**目标**：把 `config/agents/{entity-extractor,dream-evolver,journal-agent}.md` 里对 `[id:UUID] [idx:N]` 格式的描述改为"消息以 history 形式逐条传入"。同时改 `journal-agent.md:60` 的"基于消息 UUID 去重"为"基于消息内容去重"（改造后 history 无 UUID 前缀，LLM 看不到 UUID）。

- [ ] **Step 11.1**：改 `config/agents/entity-extractor.md`

old_string（L22-29）:
```
## 输入规范

- 由系统通过 task 方式自动调用，不暴露给主Agent
- 消息以文本形式内嵌在 task 中，格式为：`[id:UUID] [idx:N] Xtokens role: 内容`
- `id`：消息在数据库中的 UUID（持久标识，用于游标存储）
- `idx`：消息在全量列表中的序号（1-based，动态值，删除消息后会变）
- `Xtokens`：该条消息的 token 估算值
- 消息内容为**完整原文**，不做截断
- 你应基于传入的消息内容进行实体和关系提取
```

new_string:
```
## 输入规范

- 由系统通过 task 方式自动调用，不暴露给主Agent
- 消息以 history 形式逐条传入（task 仅含指令，不含消息文本）
- 每条消息含 `role`（user/assistant/tool）和 `content` 字段，content 前缀 `[N]` 极简编号（1-based，如 `[1] 消息内容`、`[2] 消息内容`）
- assistant 消息可能含 `tool_calls`（工具调用列表），tool 消息含 `tool_call_id`（对应父 assistant 的工具调用 ID）
- 消息内容为**完整原文**，不做截断
- 你应基于传入的消息内容进行实体和关系提取
- **处理完成后，在最终回复的最后一行输出 `processed_up_to=N`**（N 是你实际处理到的最后一条消息的编号），程序据此推进游标；如果未输出，程序会回退到区间末尾作为游标（兜底）
```

- [ ] **Step 11.2**：改 `config/agents/entity-extractor.md` 的游标机制段（L78-82）

old_string:
```
## 游标机制

- 程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息
- 每条消息带有 `[id:UUID] [idx:N]` 标注，idx 是全量列表序号（不是增量相对序号）
- 游标由程序自动推进，你无需报告游标位置
```

new_string:
```
## 游标机制

- 程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息
- history 列表中的消息即为本次需要处理的全量消息，不含已处理过的旧消息；每条 content 前缀 `[N]` 极简编号（1-based）
- 游标由程序根据你输出的 `processed_up_to=N` 推进（查映射找到对应 UUID 写入游标文件），你无需报告游标位置，但必须输出 `processed_up_to=N`
```

- [ ] **Step 11.3**：改 `config/agents/dream-evolver.md` 的游标机制段（L474-493）

old_string:
```
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息，不需要自行过滤范围。

每条消息格式为 `[id:UUID] [idx:N] Xtokens role: content`。

**重要**：
- **游标用 id（UUID）存储**：因为 id 是数据库中持久化的，删除消息不影响其他消息的 id
- **idx 是全量列表序号**：代表消息在完整对话中的位置（1-based，动态值，删除消息后会变）
- **UUID v4 字典序不代表时间先后**：不要用 id 比较大小来判断先后

**操作步骤**：
1. 直接处理收到的全部消息（程序已保证只传入增量范围内的消息）
2. 游标由程序自动推进，你无需报告游标位置

**输入规范**：
- 消息内容为**完整原文**，不做截断
- `Xtokens` 为该条消息的 token 估算值（基于完整内容计算）
- `role` 为消息角色（user / assistant / tool）
```

new_string:
```
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息，不需要自行过滤范围。

消息以 history 形式逐条传入（task 仅含指令，不含消息文本），每条 content 前缀 `[N]` 极简编号（1-based，如 `[1] 消息内容`、`[2] 消息内容`），每条含 `role` 和 `content` 字段，assistant 消息可能含 `tool_calls`，tool 消息含 `tool_call_id`。

**操作步骤**：
1. 直接处理收到的全部消息（程序已保证只传入增量范围内的消息）
2. 处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标；如果未输出，程序会回退到区间末尾作为游标（兜底）

**输入规范**：
- 消息内容为**完整原文**，不做截断
- `role` 为消息角色（user / assistant / tool）
```

- [ ] **Step 11.4**：改 `config/agents/journal-agent.md` 的输入格式段（L25-32）

old_string:
```
## 输入格式

task 中可能包含两种内容：

1. **增量消息**（包含 `[id:UUID] [idx:N]` 标注的对话消息）：从中提取工作内容，写入日志。这是最常见的场景。
2. **纯指令**（如"生成本周工作周报"）：不包含消息标注，按指令执行报告生成等操作。

如果 task 中有 `[id:UUID]` 格式的消息，按日志记录流程处理。如果没有，按指令内容执行。
```

new_string:
```
## 输入格式

task 是纯指令，消息以 history 形式逐条传入，每条 content 前缀 `[N]` 极简编号（1-based）。两种场景：

1. **日志记录**（默认）：task 是"从消息中识别工作内容..."指令，history 含增量对话消息，从中提取工作内容写入日志。这是最常见的场景。处理完成后，在最终回复的最后一行输出 `processed_up_to=N`，程序据此推进游标。
2. **报告生成**（task 明确要求"生成周报/月报/季报/年报"）：history 为空或不含相关消息，按指令执行报告生成操作。此时无需输出 `processed_up_to=`。

如果 history 含消息，按日志记录流程处理。如果 history 为空或 task 明确要求生成报告，按指令内容执行。
```

- [ ] **Step 11.5**：改 `config/agents/journal-agent.md` 的游标机制段（L74-81）

old_string:
```
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息。

每条消息格式为 `[id:UUID] [idx:N] Xtokens role: content`。

**操作步骤**：
1. 直接处理收到的全部消息
2. 游标由程序自动推进，你无需报告游标位置
```

new_string:
```
## 游标机制

程序只传入增量消息（游标之后的新消息），你只需处理收到的全部消息。

消息以 history 形式逐条传入，每条 content 前缀 `[N]` 极简编号（1-based），每条含 `role` 和 `content` 字段，assistant 消息可能含 `tool_calls`，tool 消息含 `tool_call_id`。

**操作步骤**：
1. 直接处理收到的全部消息
2. 处理完成后，在最终回复的最后一行输出 `processed_up_to=N`（N 是你实际处理到的最后一条消息的编号），程序据此推进游标；如果未输出，程序会回退到区间末尾作为游标（兜底）
```

- [ ] **Step 11.5.1**：改 `config/agents/journal-agent.md` 的写入流程段（L60）UUID 去重规则

**为什么必须改**：改造后 history 消息无 `[id:UUID]` 前缀，LLM 看不到 UUID，原"基于消息 UUID 去重"规则会让 LLM 困惑（它不知道每条消息的 UUID 是什么）。

当前代码（L60）：
```
4. 同一条消息不重复写入（基于消息 UUID 去重）
```

old_string:
```
4. 同一条消息不重复写入（基于消息 UUID 去重）
```

new_string:
```
4. 同一条消息不重复写入（基于消息内容去重 — 比对当天已有条目与 history 消息的内容，相同内容不重复追加）
```

- [ ] **Step 11.6**：改 `config/agents/journal-agent.md` 的输出格式段（L91-93）

当前代码：
```
处理范围：消息 idx {start_idx} ~ {end_idx}（共 {count} 条）
```

由于子 Agent 不再有 idx 概念，改为按消息条数描述：

old_string:
```
处理范围：消息 idx {start_idx} ~ {end_idx}（共 {count} 条）
```

new_string:
```
处理范围：共 {count} 条消息
```

- [ ] **Step 11.7**：同理改 `config/agents/dream-evolver.md` 的回复格式段（L502）

old_string:
```
处理范围：消息 idx {start_idx} ~ {end_idx}（共 {count} 条）
```

new_string:
```
处理范围：共 {count} 条消息
```

---

### Task 12: 回归测试 — 跑现有测试套件

**目标**：确保改造没破坏现有功能。

- [ ] **Step 12.1**：跑 compat 相关测试
```bash
cd <repo_root>
python -m pytest tests/test_compress_history.py tests/test_journal_agent_tidy.py tests/test_one_turn_compress.py tests/test_noncompress_subagent_history.py tests/test_compress_quality.py -v 2>&1 | tail -40
```
**预期**：所有测试通过（如果 `test_journal_agent_tidy.py` 有依赖旧 `_build_journal_task` 签名的测试，需要同步改测试）

- [ ] **Step 12.2**：如果 `test_journal_agent_tidy.py` / `agent/runner.py` / `niu_api/compat.py` 有调用依赖旧 `_build_journal_task(msg_text, safe_tokens)` 签名，或依赖旧 `_build_journal_task_for_handler` 二元组返回值，或依赖旧 `_update_journal_cursor` 二元参数，更新测试或调用点

先 grep 看有没有遗漏的调用点（**范围扩大到 `tests/ agent/ niu_api/`**，避免再次遗漏 `handler.py` / `runner.py` 这种非 tests 目录的调用点）：
```bash
cd <repo_root>
# 检查 _build_journal_task 调用点（应全部无参）
grep -rn "_build_journal_task" tests/ agent/ niu_api/
# 检查 _build_plain_history / _parse_processed_up_to import 是否齐全（compat.py / runner.py / handler.py 都应 import）
grep -rn "_build_plain_history\|_parse_processed_up_to" tests/ agent/ niu_api/
# 检查 _build_journal_task_for_handler 调用点（应解构四元组）
grep -rn "_build_journal_task_for_handler" tests/ agent/ niu_api/
# 检查 _update_journal_cursor 调用点（应传 idx_to_id 第三参数）
grep -rn "_update_journal_cursor" tests/ agent/ niu_api/
```

**预期输出**：
- `_build_journal_task`：7 处（定义 + 3 个 compat 调用点 + runner import + runner force 镜像 + handler import + handler 调用点），全部无参 `_build_journal_task()`
- `_build_plain_history`：compat.py 定义 + compat 各调用点 + runner.py import + runner 各调用点 + handler.py import + handler 调用点
- `_parse_processed_up_to`：compat.py 定义 + compat 各游标推进处 + runner.py `_run_subagent_step` 内部 import + handler.py `_update_journal_cursor` 内部 import
- `_build_journal_task_for_handler`：handler.py 定义 + `_call_subagent_gen` 调用点（解构四元组 `task, _journal_history, _journal_idx_to_id, journal_msg_ids_for_cursor`）
- `_update_journal_cursor`：handler.py 定义 + `_call_subagent_gen` 调用点（传 3 参数 `result, journal_msg_ids_for_cursor, _journal_idx_to_id`）

**如果 grep 发现还有 `_build_journal_task(...)` 带参数的调用**，说明有遗漏，必须补改（带参调用会 `TypeError`）。把遗漏的调用改为 `_build_journal_task()`（无参）。
**如果 grep 发现 `_build_journal_task_for_handler` 调用点解构的是二元组或三元组**，说明遗漏了四元组改造，必须补改。
**如果 grep 发现 `_update_journal_cursor` 调用只传 2 参数**，说明遗漏了 idx_to_id 透传，必须补改。

- [ ] **Step 12.3**：跑 subagent 相关测试
```bash
cd <repo_root>
python -m pytest tests/test_call_subagent_with_auto_answer.py tests/test_general_subagent.py tests/test_subagent_overflow.py -v 2>&1 | tail -30
```
**预期**：所有测试通过

- [ ] **Step 12.4**：跑全量测试套件
```bash
cd <repo_root>
python -m pytest tests/ -x --ignore=tests/test_async_subagent_dispatch.py 2>&1 | tail -50
```
**预期**：所有测试通过（忽略异步子 Agent 测试，它们通常需要特殊环境）

---

### Task 13: 真实端到端验证

**目标**：用真实 ./niu 触发压缩，验证 entity-extractor/dream-evolver/journal-agent 的 request.json 是逐条 history 而非单条 task。

- [ ] **Step 13.1**：清理测试环境
```bash
cd <repo_root>
# 杀掉所有 niu 进程（优雅退出，不能用 pkill -f niu）
ps aux | grep -i niu | grep -v grep | awk '{print $2}'
# 手动检查后用 kill -TERM 优雅退出
```

- [ ] **Step 13.2**：清空数据库 + 游标文件（真实测试铁律）
```bash
cd <repo_root>
# 备份当前数据库（如果有）
cp ~/.niu/messages.db ~/.niu/messages.db.bak.$(date +%Y%m%d%H%M%S) 2>/dev/null || true
# 清空游标文件
rm -f ~/.niu/last_entity_extract.json ~/.niu/last_dream_evolve.json ~/.niu/last_journal.json ~/.niu/last_compress.json
# 清空 raw_http 日志（便于定位新日志）
rm -rf logs/raw_http/20260709/
mkdir -p logs/raw_http/20260709/
```

- [ ] **Step 13.3**：启动 ./niu，制造超阈值上下文触发 force 压缩
```bash
cd <repo_root>
./niu &
# 在 UI 中持续对话，直到上下文超 80% 触发 force 压缩
# 或直接调 /api/tidy?mode=force 强制触发
```

- [ ] **Step 13.4**：检查 raw_http 日志，验证三个子 Agent 的 request.json 是 history 逐条
```bash
cd <repo_root>
# 找最新的 entity-extractor request
ls -t logs/raw_http/20260709/*.json | head -20
# 用 grep 检查：request.json 的 messages 字段应含多条 role: user/assistant/tool
# 而非单条 role: user + task 巨型字符串
grep -l "entity-extractor" logs/raw_http/20260709/*_request.json 2>/dev/null | head -5
```

**验证标准**：
1. entity-extractor 的 request.json：`messages` 数组含多条（system + task + N 条 history），而非 system + 单条巨型 task
2. dream-evolver 的 request.json：同上
3. journal-agent 的 request.json：同上（含 compat async 路径 + runner sync 路径，两条都触发 force 压缩时都应出现）
4. context-manager 的 request.json：保持不变（仍是 history 逐条）
5. 三个子 Agent 的 history 消息 content 前缀是 `[N]` 极简编号（如 `[1] 消息内容`），**不含** `[id:UUID]` / `[idx:N]` / `Ntokens` / `role:` 臃肿前缀
6. **指令与内容分离验证**：三个子 Agent 的 task（messages[1] 或 messages[2]）只含工作指令（如"从中提取有价值的内容..."）+ `processed_up_to=N` 回传说明，**不含任何消息内容**（不应出现 `[id:` / `[idx:` / 具体对话文本）
7. **主 Agent 触发 journal-agent 路径验证**（handler.py 路径）：在 UI 中让主 Agent 调用 `chat-with-journal-agent`，检查对应 request.json 也是 history 逐条 + task 纯指令（与 tidy 管道触发的结构一致）
8. **`processed_up_to=N` 回传验证**：检查子 Agent 的 response.json 是否含 `processed_up_to=N` 行；如果含，游标应推进到 idx_to_id[N] 对应的 UUID（而非区间末尾）；如果不含，游标回退到区间末尾（兜底）

- [ ] **Step 13.5**：验证游标正常推进
```bash
cd <repo_root>
cat ~/.niu/last_entity_extract.json
cat ~/.niu/last_dream_evolve.json
cat ~/.niu/last_journal.json
```
**预期**：游标推进到最新消息 ID

- [ ] **Step 13.6**：验证子 Agent 实际工作（提取/精加工/日志）
```bash
cd <repo_root>
# 检查 journal.md 是否有新条目
tail -20 ~/.niu/journal.md
# 检查 LightRAG 是否有新实体（entity-extractor 的产出）
# 检查知识图谱是否有精加工（dream-evolver 的产出）
```

- [ ] **Step 13.7**：测试完成后彻底杀进程
```bash
cd <repo_root>
# 优雅退出（铁律：不能用 pkill -f niu）
ps aux | grep -i niu | grep -v grep | awk '{print $2}' | xargs kill -TERM
# 等待 5 秒后检查是否还有残留
sleep 5
ps aux | grep -i niu | grep -v grep
```

---

### Task 14: 提交修复 + 修文件权限

- [ ] **Step 14.1**：检查工作区状态
```bash
cd <repo_root>
git status
git diff --stat
```

- [ ] **Step 14.2**：修复文件权限（铁律 #7）
```bash
cd <repo_root>
find python/bin/ -type f -exec grep -l '^#!' {} \; | xargs chmod +x 2>/dev/null || true
find ui/*/node_modules/.bin/ -type f ! -perm -u+x -exec chmod +x {} \; 2>/dev/null || true
```

- [ ] **Step 14.3**：提交修复
```bash
cd <repo_root>
git add niu_api/compat.py agent/handler.py agent/runner.py config/agents/entity-extractor.md config/agents/dream-evolver.md config/agents/journal-agent.md tests/test_noncompress_subagent_history.py
git commit -m "$(cat <<'EOF'
fix(subagent): 非压缩子 Agent 改用 history 逐条传消息 + 指令与内容彻底分离

问题：entity-extractor/dream-evolver/journal-agent 把 600 条消息拼成单条 task 字符串，
被 _truncate_task_for_subagent 砍掉末尾最新工作内容（idx 508-600 共 92 条丢失），
且每条消息前加了无用的 [id:UUID] [idx:N] Ntokens role: 前缀（占 20%+ 容量）。
更危险的是：指令和消息内容混在 task 字符串里，截断后子 Agent 可能把消息内容里的
"指令样"句子当成指令执行（journal-agent 曾把"删除多余脑区"当指令执行了十几轮）。
这三个子 Agent 不操作 message.DB，不需要坐标。

修复（指令与内容彻底分离 + 仿 context-manager 简易 ID 映射）：
1. 新增 _build_plain_history helper（构造带 [N] 极简前缀 history + idx_to_id 映射）
2. 新增 _parse_processed_up_to helper（解析子 Agent 输出的 processed_up_to=N）
3. compat.py 6 个调用点（sleep×3 + force×3）改为 history 逐条 + task 独立指令 + 解析 processed_up_to 更新游标（带 msg_ids[-1] 兜底）
4. agent/runner.py force 镜像 3 个调用点（同步版）同步改造 + 扩展 _run_subagent_step 签名透传 history + idx_to_id
5. agent/handler.py _build_journal_task_for_handler 改为返回 (task, history, idx_to_id, msg_ids) 四元组
   + _update_journal_cursor 用 _parse_processed_up_to 解析 + 查映射更新游标
   + _call_subagent_gen 接收 history + idx_to_id 透传给 call_subagent / _update_journal_cursor（主 Agent 触发 journal-agent 路径）
6. _build_journal_task 改为纯指令构造器（无参），不再嵌入 msg_text
7. 三个子 Agent 的 system prompt 同步改为 [N] 极简编号 + processed_up_to=N 回传格式
   + journal-agent.md:60 UUID 去重改为内容去重
8. context-manager 完全不动（已是正确做法）
9. _build_incremental_msg_text 和 _truncate_task_for_subagent 保留
   （前者仍用于收集 out_msg_ids + context-manager 模式一，后者仍用于 context-manager 模式一）

效果：
- 子 Agent 能看到完整的最新工作内容（不再被截断）
- 节省 ~25000 tokens 的无用前缀开销（[N] 极简前缀 vs [id:UUID] [idx:N] Ntokens role:）
- task 指令独立成消息，指令和内容彻底分离，杜绝子 Agent 误把内容当指令执行的风险
- 仿 context-manager 简易 ID 映射：子 Agent 回传 processed_up_to=N，程序据此精确推进游标（解决"部分处理"场景），带 msg_ids[-1] 兜底
- 三条路径（compat async / runner sync / handler 主 Agent 触发）全部统一为 history 逐条 + [N] 前缀 + processed_up_to=N 回传模式

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 14.4**：验证提交成功
```bash
cd <repo_root>
git log --oneline -3
git status
```
**预期**：最新 commit 是本次修复，工作区干净

---

## Self-Review

### 改动是否最小化？

- ✅ **context-manager 完全不动**：L208-2182（sleep 模式二）+ L2805-2818（force 模式）都不改
- ✅ **`_build_incremental_msg_text` 保留**：仍用于收集 `out_msg_ids`（游标推进依赖），且 context-manager 模式一仍用它构造 `compress_msg_text`
- ✅ **`_truncate_task_for_subagent` 保留**：context-manager 模式一仍用它截断 `compress_msg_text`（L2096）
- ✅ **`_build_compress_history` 保留**：context-manager 专用，加 idx 前缀是它的核心语义
- ✅ **新增 `_build_plain_history`**：只新增不修改既有函数，降低回归风险
- ⚠️ **`_build_journal_task` 签名改变**：从 `(journal_msg_text, safe_tokens=0)` 改为 `()`，需要检查测试是否有依赖（Task 12.2 已覆盖，grep 范围 `tests/ agent/ niu_api/`）
- ⚠️ **`_run_subagent_step` 签名扩展**：新增 `history=None, context_fifo_threshold=None, idx_to_id=None` 三个可选参数，向后兼容（不传时行为不变：history 不透传、游标走 fallback_ids[-1] 兜底），无回归风险

### 三个子 Agent 的改造是否结构一致？

- ✅ 都用 `history=...` + `task=纯指令` + `context_fifo_threshold=0`
- ✅ 都用 `_build_plain_history` 构造 history + 返回 `idx_to_id` 映射（解构元组）
- ✅ 都用 `_build_incremental_msg_text` 收集 `out_msg_ids`（丢弃文本）
- ✅ entity force 用全量 history（cursor=""）**+ 排除 PROTECTED 最近 N 条**（方案 A，详见 Architecture §6），dream/journal force 用增量 history（按 msg_ids 过滤，不排除 PROTECTED）
- ✅ 都在游标推进处用 `_parse_processed_up_to` 解析输出 + 查 `idx_to_id` 映射 + `msg_ids[-1]` 兜底
- ✅ 三条路径（compat async 6 点 / runner sync 3 点 / handler 主 Agent 触发 1 点）全部统一为 history 逐条 + `[N]` 前缀 + `processed_up_to=N` 回传模式
- ✅ **Task 9 handler `_build_journal_task_for_handler` 三个返回路径全部改四元组**（L833 报告生成 + L848 无消息 + L876-882 主返回），避免 `_call_subagent_gen` 解构 `ValueError`（第二轮审查问题 2 修复）

### system prompt 修改是否完整？

- ✅ entity-extractor.md：输入规范段（含 `[N]` 编号说明 + `processed_up_to=N` 回传格式）+ 游标机制段（含"游标由程序根据 `processed_up_to=N` 推进"）
- ✅ dream-evolver.md：游标机制段（含 `[N]` 编号 + `processed_up_to=N`）+ 回复格式段
- ✅ journal-agent.md：输入格式段（含 `[N]` 编号 + `processed_up_to=N`，区分日志记录/报告生成两种场景）+ 游标机制段 + 输出格式段 + **写入流程段 L60 UUID 去重改为内容去重**（Task 11.5.1）
- ⚠️ **需确认**：entity-extractor.md 的"输出示例"段（L56-69）不含 idx 引用，无需改；dream-evolver.md 的"工作流程"段（L142-164）不含 idx 引用，无需改

### 测试是否覆盖关键路径？

- ✅ `_build_plain_history` 基本构造 + 返回 idx_to_id 元组（test_build_plain_history_basic_and_idx_to_id）
- ✅ `_build_plain_history` 保留 tool_calls/tool_call_id + `[N]` 前缀（test_build_plain_history_preserves_tool_calls）
- ✅ content 前缀是极简 `[N]` 而非 `[id:UUID]`/`[idx:N]`/`Ntokens`/`role:`（test_build_plain_history_prefix_is_minimal_not_verbose）
- ✅ `_parse_processed_up_to` 解析各种格式（= / : / 空格，大小写不敏感）（test_parse_processed_up_to_various_formats）
- ✅ `_parse_processed_up_to` 未找到返回 None（test_parse_processed_up_to_not_found_returns_none）
- ✅ 空消息列表 + out_msg_ids=None 默认值（test_build_plain_history_empty_messages / test_build_plain_history_out_msg_ids_default_none）
- ⚠️ **未覆盖**：9 个调用点 + handler 路径的集成测试（需要 mock call_subagent_with_auto_answer 验证传入的 history 参数 + 解析 processed_up_to 更新游标）—— 这类测试容易过拟合实现细节，本次靠真实端到端验证（Task 13）覆盖

### 有没有引入新 bug 的风险？

- ⚠️ **风险 1：`context_fifo_threshold=0` 关闭 FIFO 后，子 Agent 上下文超限怎么办？**
  - context-manager 已用 `context_fifo_threshold=0` 长期运行，证明可行
  - 子 Agent 的 history 是增量消息（dream/journal）或全量消息（entity force），单次调用通常不会超限
  - 如果超限，`agent_runner_loop` (L757-772) 检测 `context_overflow` 返回 `CONTEXT_OVERFLOW`，`_is_subagent_overflow` 返回 True，游标不推进，下次重跑——这是已有机制，无新增风险
  - **entity force 全量路径特殊风险**：600 条消息全量进 history 可能 overflow → 游标不动 → 下次 force 再 overflow → **死循环**。**已用方案 A 缓解**（entity force 排除 PROTECTED 最近 N 条，与 context-manager 对齐，详见 Architecture §6 + Task 6.0/6.2 + Task 10.2）

- ⚠️ **风险 1.5：`_parse_processed_up_to` 正则是否覆盖所有分隔格式？**
  - 正则 `r'processed_up_to\s*[=:\s]\s*(\d+)'`（字符类 `[=:\s]` 含 `=`、`:`、空格三种分隔）
  - Task 1.1 测试覆盖：`processed_up_to=15` / `processed_up_to: 15` / `processed_up_to 15` / 大小写不敏感 / 第一个有效整数
  - docstring 声称"支持 `=`/`:`/空格分隔"，实现与之一致（第二轮审查问题 1 修复）

- ⚠️ **风险 2：去掉 idx 后，子 Agent 输出报告里的 `idx` 引用会失效吗？**
  - dream-evolver.md 的回复格式段原本有 `处理范围：消息 idx {start_idx} ~ {end_idx}`，本次改为 `共 {count} 条消息`
  - journal-agent.md 同上
  - entity-extractor.md 的输出示例不含 idx，无需改
  - **已覆盖**（Task 11.6 / 11.7）

- ⚠️ **风险 3：`_build_journal_task` 签名改变，其他调用点是否都已更新？**
  - grep `tests/ agent/ niu_api/` 确认共 7 处：L853（定义）+ compat.py L2004（sleep）+ compat.py L2704（force）+ runner.py L1036（import）+ runner.py L1187（force 镜像）+ handler.py L827（import）+ handler.py L882（handler 路径）
  - Task 5.2 改 compat sleep 调用，Task 8.2 改 compat force 调用，Task 9.1 改 handler 调用，Task 10.4 改 runner force 镜像调用，全部覆盖
  - Task 12.2 的 grep 会再次验证无遗漏

- ⚠️ **风险 4：`_build_plain_history` 不排除孤立 tool，子 Agent 会看到没有父 assistant 的 tool 消息吗？**
  - 不会。`_build_incremental_msg_text` 收集 `out_msg_ids` 时按游标范围取连续消息，tool 消息必然跟在父 assistant 后面
  - 即使有边缘情况（如父 assistant 在游标之前，tool 在游标之后），子 Agent 看到 tool 消息也能处理（content 里有工具输出，不影响理解）
  - context-manager 用 `_build_compress_history` 排除孤立 tool 是因为它的 keep=/update= 解析需要 idx 严格连续，非压缩子 Agent 没这个约束

- ⚠️ **风险 5：`agent/handler.py` 的 `_call_subagent_gen` 透传 history + idx_to_id 是否正确？**
  - `_build_journal_task_for_handler` 改为返回 `(task, history, idx_to_id, msg_ids)` 四元组后，`_call_subagent_gen` 必须接收 history 透传给 `call_subagent(history=...)`，同时接收 idx_to_id 透传给 `_update_journal_cursor(result, msg_ids, idx_to_id)`
  - **三个返回路径全部改四元组**（L833 报告生成 + L848 无消息 + L876-882 主返回），避免解构 `ValueError`（第二轮审查问题 2 修复）
  - Task 9.1.1 改造了 `_update_journal_cursor` 新增 `journal_idx_to_id` 参数 + `_parse_processed_up_to` 解析逻辑
  - Task 9.2 已覆盖，但具体 old_string/new_string 需执行子 Agent 先 Read `handler.py:995-1010` 确认实际代码（该段当前 `_history` 来源 + `_update_journal_cursor` 调用点未在计划中硬编码，避免过拟合）
  - **验证**：Task 13 端到端测试时，主 Agent 调用 `chat-with-journal-agent` 触发 journal-agent（含报告生成场景），检查 raw_http 日志的 request.json 是否含多条 history + response.json 含 `processed_up_to=N` + 游标文件推进到对应 UUID；**额外验证报告生成场景**（task 含"周报"/"月报"关键词）不崩溃（四元组早返回路径正常）

- ⚠️ **风险 6：`agent/runner.py` 的 `_run_subagent_step` 扩展后，原调用点（非本次改造的）是否受影响？**
  - `_run_subagent_step` 新增的三个参数 `history=None, context_fifo_threshold=None, idx_to_id=None` 默认值都是 `None`，不传时 `call_subagent_with_auto_answer` 不会收到 history/context_fifo_threshold（因 `_kwargs` 只在非 None 时加入），游标推进走 `fallback_ids[-1]` 兜底（因 `idx_to_id` 为 None 时 `_parse_processed_up_to` 查不到映射），行为与改造前完全一致
  - **无回归风险**

- ⚠️ **风险 7：子 Agent 不遵循 `processed_up_to=N` 格式怎么办？**
  - 已有兜底：`_parse_processed_up_to` 返回 None 时，游标回退到 `msg_ids[-1]`（与改造前行为一致）
  - LLM 不遵循格式只是"无法精确推进游标"，不会导致游标丢失或卡死
  - system prompt 已明确说明格式 + 兜底机制，LLM 遵循概率高
  - **无致命风险**，最坏情况退化为改造前的"区间末尾推进"行为

---

## Execution Handoff

本计划共 15 个 Task（Task 0-14），每个 Task 的 Step 都有完整代码，无占位符（Task 9.2 除外，需执行子 Agent 先 Read 确认 handler.py 实际代码）。

**执行顺序**：Task 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14

**关键检查点**：
- Task 1 完成后：8 个测试全失败（`_build_plain_history` / `_parse_processed_up_to` 不存在）
- Task 2 完成后：8 个测试全通过
- Task 8 完成后：测试仍全通过（compat.py 6 个调用点改完 + 游标推进逻辑改造完）
- Task 9 完成后：`python -c "import agent.handler"` 无异常（handler.py 四元组 + _update_journal_cursor + _call_subagent_gen 改完）
- Task 10 完成后：`python -c "import agent.runner"` 无异常（runner.py force 镜像改完 + _run_subagent_step 扩展 + 内部游标推进改造完）
- Task 11 完成后：三个子 Agent 的 system prompt 含 `[N]` 编号 + `processed_up_to=N` 回传说明 + journal-agent.md:60 UUID 去重改完
- Task 12 完成后：现有测试套件全通过 + grep 确认无遗漏的 `_build_journal_task(...)` 带参调用 + `_build_journal_task_for_handler` 解构四元组 + `_update_journal_cursor` 传 3 参数
- Task 13 完成后：真实 ./niu 触发压缩，raw_http 日志验证三条路径的 history 逐条 + `[N]` 前缀 + response 含 `processed_up_to=N` + 游标按映射推进
- Task 14 完成后：提交成功，工作区干净

**派给子 Agent 的约束**：
- 必须遵守所有铁律（修改前备份 / 禁止 git reset --hard / 真实数据测试 / git 操作后修权限）
- 每个 Step 的代码必须原样执行，不能自作主张改方向
- 如果发现计划有偏差，停下来报告，不要自作主张改方向
