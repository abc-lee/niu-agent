# 子 Agent FIFO 保底 + dream-evolver 拆分设计

## 问题

### 问题 1：FIFO 被关闭导致溢出死循环

`niu_api/compat.py` 中所有 9 个子 Agent 调用点都显式传 `context_fifo_threshold=0`，关闭了 FIFO 的 fallback truncation 路径。

FIFO 机制有两条触发路径（`agent/generic/agent_loop.py`）：

1. **Fallback truncation**（L674-679）：首轮触发（`last_prompt_tokens==0`），条件 `context_fifo_threshold > 0`。`context_fifo_threshold=-1` 时 `fifo_threshold = context_window_tokens * 0.75`。
2. **Proactive pruning**（L654-673）：多轮后使用率超 80% 时触发，不依赖 `context_fifo_threshold`。裁剪到 30% target。

当 `context_fifo_threshold=0` 时：fallback truncation 关闭，proactive pruning 仍工作。

**致命场景**：如果子 Agent 的 history 本身就接近或超过上下文窗口（如 force 模式下 entity-extractor 传全量消息），第一轮 LLM 调用就 `context_length_exceeded` → `CONTEXT_OVERFLOW` → 游标不动 → 下次重跑相同范围 → **死循环**。proactive pruning 来不及救——它只在 `last_prompt_tokens > 0` 时触发，但第一轮就溢出了。

改为 `context_fifo_threshold=-1` 后：首轮 fallback truncation 先裁剪到 75%，确保第一轮 LLM 调用不溢出。后续轮次 proactive pruning 继续保护。

**代价**：FIFO 顶出旧消息可能丢失上下文。但对所有子 Agent 来说，溢出终止（游标死循环）比丢失部分旧消息更严重——丢失只是遗漏，溢出是损坏。

### 问题 2：dream-evolver 多任务串行模式上下文压力大

dream-evolver 的工作流程是多任务串行：
- 阶段A：逐条阅读全部消息，提取实体和 skill 信号
- 阶段B：对阶段A提取的实体做精加工——4 个步骤串行，每步调用 `lightrag_search_entities` 等图谱工具
- 阶段C：Skill 操作——读取/编辑 skill 文件

与 entity-extractor / journal-agent 的线性流式不同（逐条处理→入库→结束），dream-evolver 的阶段B/C 需要频繁调用图谱工具，工具返回结果累积在上下文里。即使 FIFO 开启，阶段A的消息 + 阶段B/C 的工具调用结果仍可能溢出。

**拆分方案**：当 dream-evolver 增量消息的 token 总量占子 Agent 上下文窗口的 50% 以上时，将增量消息范围在 user 消息边界处拆成两批，分两轮调用。每轮独立处理一半消息，各自推进游标。

**为什么 50%**：`TokenCalculator` 的本地 tokenizer 与实际 API tokenizer 存在偏差，估算值不精确。50% 比 60% 更保守，留出更多余量抵消估算误差。此外 dream-evolver 的工具调用累积（阶段B反复 `lightrag_search_entities`）比其他子 Agent 更多，50% 阈值意味着消息占一半，剩下 50% 留给 system prompt + 工具 schema + 工具调用结果，足够宽裕。

**为什么不用主 Agent 的 `usage_percent`**：`usage_percent` 是主 Agent 的上下文使用率，它高不代表 dream-evolver 的增量范围大。反过来，如果 dream-evolver 游标很久没推进（比如前几次都 overflow），积攒了大量未处理消息，即使主 Agent usage 不高，dream-evolver 的增量范围也可能很大。正确的判断依据是 dream-evolver 实际收到的增量消息的 token 量占子 Agent 上下文窗口的比例。

**为什么只拆 dream-evolver**：
- entity-extractor：线性流式（逐条处理→`lightrag_insert`→结束），FIFO 顶出旧消息不影响后续处理
- journal-agent：线性流式（逐条处理→追加写入 journal.md→结束），同上
- dream-evolver：多任务串行，阶段B/C 需要消息上下文 + 工具调用结果累积，FIFO 顶出可能丢失阶段A的上下文

## 方案

### Part 1：全部子 Agent 开启 FIFO 保底

将 `niu_api/compat.py`（9 处）和 `agent/runner.py`（4 处）中所有 13 处 `context_fifo_threshold=0` 改为 `context_fifo_threshold=-1`。

`-1` 的含义（`agent/subagent.py` L800-801）：`fifo_threshold = int(context_window_tokens * 0.75)`，即首轮裁剪到 75%。

涉及调用点：

**compat.py 9 处**：

| # | 行号 | 子 Agent | 模式 | history 传入 |
|---|------|---------|------|-------------|
| 1 | L2418 | entity-extractor | sleep | 增量 history |
| 2 | L2499 | dream-evolver | sleep | 增量 history |
| 3 | L2581 | journal-agent | sleep | 增量 history |
| 4 | L2752 | context-manager | sleep 模式二 | compress_history |
| 5 | L2971 | context-manager | sleep 模式一 | 无（只传 task） |
| 6 | L3146 | entity-extractor | force | 全量 history（排除 PROTECTED） |
| 7 | L3225 | dream-evolver | force | 增量 history |
| 8 | L3307 | journal-agent | force | 增量 history |
| 9 | L3419 | context-manager | force | _force_history |

**runner.py 4 处**（`_on_context_high_usage` 回调中的 force 压缩路径，R3 审查发现）：

| # | 行号 | 子 Agent | 模式 | history 传入 |
|---|------|---------|------|-------------|
| 10 | L1238 | entity-extractor | force | 全量 history（排除 PROTECTED） |
| 11 | L1277 | dream-evolver | force | 增量 history |
| 12 | L1313 | journal-agent | force | 增量 history |
| 13 | L1384 | context-manager | force | _force_history |

注：L2971（context-manager 模式一）不传 history，FIFO 对它无实际效果，但统一改为 `-1` 保持一致性。runner.py 的 `_on_context_high_usage` 是主 Agent 上下文超 80% 时自动触发的 force 压缩路径，与 compat.py 的 `_tidy_context_impl(mode="force")` 是并行实现。

### Part 2：dream-evolver 拆分调用

当 dream-evolver 增量消息的 token 总量占子 Agent 上下文窗口的 50% 以上时，将增量消息拆成两批调用。程序只负责"砍一半"生成第一批的范围，第二批的范围由大模型输出的 `processed_up_to` 动态决定。

#### 拆分算法

新增辅助函数 `_split_dream_first_batch`：

```python
def _split_dream_first_batch(
    messages: list,
    dream_msg_ids: list[str],
    msg_tokens: list[int],
    context_window_tokens: int,
    threshold: float = 0.50,
) -> list[str] | None:
    """计算 dream-evolver 第一批的消息 ID 列表。

    当增量消息的 token 总量 >= 上下文窗口的 threshold 时，
    在中间位置向两端查找最近的 role=user 消息作为分割点，
    返回第一批的消息 ID 列表（split_pos 之前，不含 user 消息）。
    无需拆分时返回 None。
    """
```

算法步骤：

1. 计算增量消息 token 总量：遍历 `messages`，对 `msg.id in dream_msg_ids` 的消息累加 `msg_tokens[i]`
2. 如果 `incremental_tokens < context_window_tokens * threshold` 或 `len(dream_msg_ids) < 4`，返回 `None`（不拆分）
3. 构建增量消息子列表 `dream_incremental_msgs`（保持原序）
4. 取中间位置 `mid = len(dream_incremental_msgs) // 2`
5. 从 `mid` 向两端查找最近的 `role=user` 消息：
   - 向右找：从 `mid` 到末尾，找第一个 `role=user` 的位置 `right_user`
   - 向左找：从 `mid` 到开头，找第一个 `role=user` 的位置 `left_user`
   - 选择更接近 `mid` 的作为分割点 `split_pos`
   - 如果只找到一侧有 user，用那个位置
   - 如果都没有 user（纯 tool/assistant 消息），返回 `None`（不拆分）
6. 返回 `dream_msg_ids[0:split_pos]`（第一批，不含 user 消息）

**分割点在 user 消息处**：`split_pos` 指向 user 消息，第一批包含 user 消息之前的所有消息（不含该 user）。第二批从该 user 消息开始（含该 user）——但第二批的确切范围由第一批处理后的 `processed_up_to` 动态决定。

#### 调用流程

将 dream-evolver 的调用逻辑从"单次调用"改为"两批调用"：

```
first_batch_ids = _split_dream_first_batch(messages, dream_msg_ids, msg_tokens, context_window_tokens)
if first_batch_ids is None:
    # 不拆分，正常单次调用
    call dream-evolver with all dream_msg_ids
else:
    # 第一批
    first_msgs = filter messages by first_batch_ids
    first_history, first_idx_to_id = _build_plain_history(first_msgs)
    call dream-evolver with first_history
    if overflow: break (游标不动)
    parse processed_up_to=N → new_dream_id = first_idx_to_id[N]
    write cursor
    last_dream_evolve_id = new_dream_id

    # 第二批：从游标之后到末尾，动态计算
    second_batch_ids = [mid for mid in dream_msg_ids if mid is after new_dream_id in messages order]
    if second_batch_ids:
        second_msgs = filter messages by second_batch_ids
        second_history, second_idx_to_id = _build_plain_history(second_msgs)
        call dream-evolver with second_history
        parse processed_up_to → write cursor
```

**关键设计**：第二批的范围不固定——如果大模型在第一批中只处理了 90/110 条（输出 `processed_up_to=90`），第二批从第 91 条开始到末尾，包含第一批未处理的 20 条 + 原计划后半段。大模型自己判断哪些处理了，程序据此动态计算第二批。

#### 适用范围

- sleep 路径 dream-evolver（compat.py L2477-2538）
- force 路径 dream-evolver（compat.py L3203-3252）
- force 路径 dream-evolver（runner.py L1264-1285，R3 审查发现）
#### 修改 dream-evolver 提示词

task prompt 做了两处修改：
1. 方向词修正：「对以下消息」→「对以上消息」（消息以 history 形式传入，大模型看到的是以上内容）
2. 增加对话单元完整性指导：如果最后一段不是完整的对话单元（如 assistant 回复未完成、tool 调用缺少对应结果），请将 `processed_up_to` 设为你最后完整处理到的那条消息的编号，不要设到不完整的位置。

dream-evolver 对每批消息独立执行阶段A→B→C，语义正确。大模型自己判断处理到哪条，输出 `processed_up_to=N`，程序据此推进游标并计算第二批范围。

## 修改文件

| 文件 | 修改内容 |
| `niu_api/compat.py` | 9 处 `context_fifo_threshold=0` → `-1`；新增 `_split_dream_first_batch` 函数；dream-evolver sleep/force 路径增加拆分逻辑；entity-extractor/journal-agent prompt 方向词修正 |
| `agent/runner.py` | 4 处 `context_fifo_threshold=0` → `-1`；dream-evolver force 路径增加拆分逻辑；entity-extractor prompt 方向词修正 |
| `tests/test_dream_split.py` | 新建，测试 `_split_dream_first_batch` 拆分算法 |

## 风险

1. **FIFO 顶出 dream-evolver 阶段A消息**：阶段B/C 可能丢失阶段A提取的实体上下文。但比溢出终止好——遗漏 < 损坏。拆分方案进一步缓解此问题。
2. **FIFO 顶出 context-manager 消息**：context-manager 可能看不到部分消息导致压缩决策不完整。但 context-manager 的 history 已经排除了 PROTECTED 消息，且 FIFO 只在使用率超 80% 时触发，此时不裁剪也会溢出。
3. **`_fifo_prune` 的 `protect_end=2` 对子 Agent 不够保护**：子 Agent messages = `[system, history..., user(task)]`，`protect_end=2` 只保护 system 和 history 第一条。这是 `_fifo_prune` 的既有行为，不在本次修改范围。
4. **拆分后两批的实体精加工可能不完整**：第一批的实体可能引用第二批的消息上下文。但 dream-evolver 的阶段B是"对阶段A提取的实体做精加工"——每批独立提取实体并精加工，不存在跨批依赖。第二批的实体会独立处理，不会因为缺少第一批的消息而失败。
5. **TokenCalculator 估算偏差**：本地 tokenizer 与 API tokenizer 存在偏差，50% 阈值已留出余量。如果偏差过大导致误判，FIFO 机制作为保底仍能防止溢出终止。
