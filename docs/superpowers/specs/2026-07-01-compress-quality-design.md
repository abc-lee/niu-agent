# context-manager 压缩质量修复设计

**日期**: 2026-07-01
**状态**: 已审查通过，待写实施计划

## 背景

context-manager 压缩功能（模式二睡眠触发 / 模式三 force 强制触发）存在严重质量缺陷。2026-06-30 模式二实际压缩把 390+ 条消息压成 4 条 keep（实际历史 3 条）+ 6 条 update 摘要，其中 update[1] 把 290 条消息塞进一句话摘要，基本等同删除，用户记忆全丢。

## 根因

经 4 个 Agent 多方位审查确认：

1. **task prompt 丢失压缩方法论**：`niu_api/compat.py` 模式二（L1860-1882）和模式三（L2511-2548）的 task prompt 把 system prompt（`config/agents/context-manager.md`）里的完整压缩规则简化成了"按事务合并"6 条极简规则，丢失了：
   - 三区划分（最早/中间/最近）
   - 会话单元边界（2-15 条）
   - 禁止摘要无限衰减
   - 50 字符下限
   - 摘要格式规范
   - 压缩强度量化
   - 逐区对账逻辑

2. **`_compress_target` 算了但没用**：compat.py L1827-1834 构造了完整的目标 token + 逐区释放指令，但模式二 task prompt 根本没引用，只有模式一用了。

3. **配置用百分比不科学**：`targetThreshold: 0.30`（压到上下文 30%）跟模型输出能力无关，换模型要重新调百分比。

4. **无输出截断保底**：LLM 单轮输出被截断（`finish_reason=length`）时程序不检测不降级，直接拿残缺输出解析，导致 keep/update 不完整。

5. **校验兜底反而削弱约束**：update idx 不在 keep 时自动补进 keep、越界静默丢弃等"自动修正"逻辑，给 LLM 留了"犯错后程序补救"的后门，削弱 prompt 约束力。

## 设计原则

1. **靠 prompt 约束 LLM 一次做对**，不靠程序硬约束+重试（重试会触发第二轮，可能上下文溢出）
2. **程序只做技术性保底**：输出截断时应急清空（保留最近 10 条，靠 journal.md + 知识图谱回溯历史），不重试不降级
3. **配置与模型解耦**：直接写 token 数，不写百分比
4. **方法论写回 task prompt**：把用户的完整压缩方法论（三区逐份处理 + 会话单元 + 旧摘要关联性判断）写回 task prompt
5. **引入 `<analysis>` 草稿块**：让 LLM 在单轮内先分析再输出，强制思考过程外化
6. **删除校验兜底**：LLM 输出什么就用什么，不自动修正（避免削弱 prompt 约束）

## 修复设计

### 1. 配置层变更

`config/user-config.json` 的 `context` 段：

**当前**:
```json
{
  "context": {
    "warningThreshold": 0.80,
    "targetThreshold": 0.30,
    "contextWindowSize": 200000
  }
}
```

**改为**:
```json
{
  "context": {
    "warningThreshold": 0.80,
    "compressTargetTokens": 60000,
    "contextWindowSize": 200000
  }
}
```

字段说明：
- `compressTargetTokens`：压缩后上下文目标 token 数（替换 `targetThreshold`）
- `maxOutputTokens`：**不配置**——程序读 `contextWindowSize × 0.16` 动态算，封顶 65536。换模型自动适配，不依赖用户手设（详见第 3 节"max_tokens 动态计算"）
- `warningThreshold` 保留百分比（80% 触发压缩，与上下文窗口比例相关）
- `contextWindowSize` 保留

### 2. Prompt 层：task prompt 重写

模式二和模式三的 task prompt 共用方法论骨架，模式三多 cursor 行和 dream 安全边界。

#### task prompt 结构（5 部分）

```
[1] 禁工具前言
[2] 输出格式（analysis 块 + keep/update[/cursor] 行）
[3] 压缩方法论（核心）
[4] 当前上下文状态
[5] 模式特有内容（仅模式三：cursor 说明 + dream 安全边界）
```

#### [1] 禁工具前言

```
CRITICAL: 你只有一轮机会完成压缩决策。禁止调用任何工具。
- 不调用 write、delete_messages、update_message、bash 等
- 你的回复必须包含 <analysis> 块和 keep=/update=[/cursor=] 行
- 调用工具会被拒绝，浪费唯一一轮，任务失败
```

#### [2] 输出格式

```
先在 <analysis> 块里写分析过程，然后输出 keep=/update=[/cursor=] 行。

<analysis> 块内容：
- 列出三份的 idx 范围
- 估算每份删工具输出 + 合并会话单元能释放多少 token
- 判断第一份的旧摘要与近期工作的关联性
- 决定每份的处理强度

输出格式：
keep=1,3,5-10,15
update=2|[摘要] 摘要内容;11|[摘要] 摘要内容
[cursor=15]  （仅模式三）

说明：
- keep= 保留的消息 idx（逗号分隔，连续用短横线如 5-10）
- update= 需压缩为摘要的消息（idx|摘要内容，多条用分号分隔）
- update 的 idx 必须在 keep 中（update 的消息保留但 content 改为摘要）
- [cursor= 操作范围内 idx 最大且仍存在的消息 idx]  （仅模式三）
- 未列在 keep 中的消息将被删除

示例：
<analysis>
第一份 idx 1-100：含 3 个会话单元（智能家居调试/知识图谱/周报），旧摘要 5 条
其中 2 条与近期无关可删，估算释放 8K tokens
第二份 idx 101-200：估算释放 3K tokens
累计 11K，已达目标 10K，第三份轻度处理
</analysis>

keep=1,5,15,30,50,75,100,105,115,150,180,200
update=1|[摘要] 智能家居调试 → 完成 | 微波炉/空调测试;5|[摘要] ...
cursor=200
```

#### [3] 压缩方法论（核心）

```
压缩方法论（必须在一轮内完成，禁止多轮）：

1. 估算：当前 {display_tokens} tokens，目标 {compressTargetTokens} tokens，
   需释放 {display_tokens - compressTargetTokens} tokens。

2. 划分优先级（按 idx 范围，粗粒度）：
   - 第一份（最早）：idx 最小的约 1/3 范围
   - 第二份（中间）：中间约 1/3 范围
   - 第三份（最近）：idx 最大的约 1/3 范围
   注：划分是优先级提示，实际处理按会话单元边界，
   不得切断一个完整的会话单元（单元跨越划分边界时，
   整个单元归入更早的那份）。

3. 逐份处理（在 analysis 块里思考，一次输出结果）：
   a. 第一份（最早）最激进：
      - role=tool 的工具输出：全删（不进 keep）
      - 原始对话：按会话单元（2-15 条一个话题）合并，
        每个会话单元保留 1 条（锚 idx），content 改为摘要，其余删除
      - 旧摘要（已是 [摘要] 开头）：判断与近期工作的关联性，
        无关的直接删除，相关的保留
   b. 估算累计释放量。若已达目标，第二份/第三份按"轻度处理"
      （仅删工具输出、保留原文）即可。
   c. 若未达目标，处理第二份（中间）：
      - role=tool 工具输出：全删
      - 对话：按会话单元合并为摘要
      - 已有摘要：保留不动（禁止二次压缩）
   d. 再估算。若仍未达目标，处理第三份（最近）：
      - role=tool 工具输出：全删
      - 对话：仅精简超长内容，优先保留原文
   e. 若三份处理完仍未达目标，接受当前结果（受保护消息已排除）

4. 硬约束：
   - 每个会话单元至少保留 1 条（不得把多个会话单元合并成 1 条）
   - 摘要长度 ≤ 150 字符，不得低于 50 字符
   - 已是摘要（≤50 字符且信息密度高）不再二次压缩
   - update 的 idx 必须在 keep 中
   - 摘要格式：[摘要] <用户意图> → <执行结果> | <关键细节>
```

#### [4] 当前上下文状态

```
当前上下文状态：
- 参与压缩的消息数：{len(history)}（受保护消息已排除）
- 当前 token 总数：{display_tokens}（{usage_percent:.1f}%）
- 目标 token 总数：{compressTargetTokens}
- 需释放至少 {display_tokens - compressTargetTokens} tokens
- 上次压缩游标：{last_compress_id or '（无，从最早消息开始）'}  （仅模式三）

上方历史消息每条开头带 [idx:N] Ntokens 前缀，共 {len(history)} 条。
role=tool 的工具输出会被程序自动删除，不需要放入 keep。
```

#### [5] 模式三特有内容

```
安全边界：idx > {dream_idx} 的消息（dream-evolver 未提取知识），
不得直接删除，必须用 update 压缩为[摘要]格式后保留（不删除）。
注：受保护消息已从列表中排除，无需处理。

请按照【模式三】执行压缩决策，安全边界优先于模式三决策流程。
REMINDER: 禁止调用任何工具，直接在回复中输出 <analysis> 块和 keep=/update=/cursor= 三行。
```

模式二 [5] 段替换为：
```
REMINDER: 禁止调用任何工具，直接在回复中输出 <analysis> 块和 keep=/update= 两行。
```

### 3. 程序保底：输出截断检测 + 应急清空

#### 设计思路

LLM 输出截断（`finish_reason=length`）时**不重试、不降级重压**。原设计的降级重压（每次降 50% 目标）方向反效果——目标降得越低意味着要释放更多 token，LLM 反而要写更长的 analysis 说明更多压缩决策，输出更可能再次截断。

改为**单次调用 + 应急清空**：截断时直接放弃压缩，触发应急清空逻辑（保留最近 10 条，上面全删，最旧那条改为"压缩失败，历史信息丢失"摘要），写回 DB。

**应急清空的安全性保证**：我们比 Claude Code 多三层前置兜底（entity-extractor / dream-evolver / journal-agent），重要内容已入知识图谱 / journal.md。即使压缩失败应急清空，主 Agent 也能通过 journal.md + 知识图谱读回历史。用户已改 `niu.md` 让主 Agent 读 journal.md 自我修复旧记忆。`/new` 是用户功能，压缩函数内部做应急清空，不调用外部机制。

#### 检测输出截断

当前代码不捕获 `finish_reason`，需要新增传递链。改造涉及 3 个文件：

**改造 1：`agent/generic/llmcore.py` 的 `MockResponse` 增加 `finish_reason` 字段**

```python
class MockResponse:
    def __init__(self, content="", ...):
        ...
        self.finish_reason = None  # 新增，由 litellm_adapter 流式循环填充
```

**改造 2：`agent/generic/litellm_adapter.py` 流式循环捕获 `finish_reason`**

在流式 chunk 循环（L434-521 附近）里，每个 chunk 检查 `choices[0].finish_reason`，最后一个非空的 finish_reason 保留；构造 `MockResponse`（L578-583）时传入：

```python
# 流式循环里捕获
last_finish_reason = None
for chunk in response:
    ...
    if chunk.choices and chunk.choices[0].finish_reason:
        last_finish_reason = chunk.choices[0].finish_reason

# 构造 MockResponse 时传入
mock_response = MockResponse(content=..., ...)
mock_response.finish_reason = last_finish_reason or "stop"
```

**改造 3：`agent/generic/agent_loop.py` 把 `finish_reason` 放进 `return_value`**

`agent_runner_loop` 的所有 `return_value` dict 里增加 `finish_reason` 字段，从 `response.finish_reason` 取值：

```python
return {"result": "CURRENT_TASK_DONE", "data": ..., "finish_reason": response.finish_reason}
```

**注意**：`response` 变量在 L331/L336 赋值后在该函数作用域内可见，正常退出路径（L570/L583 纯文本回复无工具调用，这是 context-manager 禁工具模式实际触发的路径）return 时可直接引用 `response.finish_reason`。MAX_TURNS_EXCEEDED 路径（L610）没有 response，`finish_reason` 置 None。

**改造 4：`agent/subagent.py` 的 `call_subagent` 检测截断**

```python
def call_subagent(...) -> str:
    ...
    result_text, return_value = _run_agent_loop(...)
    
    # 检测输出截断（finish_reason == "length"）
    if return_value and isinstance(return_value, dict):
        if return_value.get("finish_reason") == "length":
            logger.warning(f"[SubAgent] {agent_name}: Output truncated (finish_reason=length)")
            return "COMPACT_TRUNCATED"
    
    # 原有逻辑：提取结构化结果
    ...
    return result_text
```

**传递路径**：`litellm` 流式 chunk → `litellm_adapter` 捕获 finish_reason → `MockResponse.finish_reason` → `agent_runner_loop` 放进 return_value → `_run_agent_loop` 透传 → `call_subagent` 检测并返回 `"COMPACT_TRUNCATED"` 字符串 → `compat.py` 识别字符串触发应急清空。

用字符串 `"COMPACT_TRUNCATED"` 而非新异常类型，避免改动 `call_subagent` 的返回签名（当前返回 str）。compat.py 里检查 `result == "COMPACT_TRUNCATED"` 触发应急清空。

#### 应急清空逻辑

`niu_api/compat.py` 模式二/模式三分支**单次调用** call_subagent，截断时触发应急清空：

```python
_compress_target_tokens = read_compress_target_tokens()  # 配置值，如 60000
prompt = build_prompt(_compress_target_tokens, ...)
result = call_subagent(
    agent_name="context-manager",
    task=prompt,
    history=history,
    llm_config=llm_config_with_max,  # 带 max_tokens
    ...
)

if result == "COMPACT_TRUNCATED":
    logger.warning("[Compact] Output truncated, triggering emergency clear")
    # 应急清空：保留最近 10 条，上面全删，最旧那条改摘要
    _emergency_clear(history, protect_recent_count=10, store=...)
    return {"status": "skipped", "mode": mode, "reason": "truncated, emergency cleared"}

# 正常返回，剥离 analysis + 解析 keep/update/cursor
response = _strip_analysis(result)
parse_and_execute(response)
```

#### 应急清空函数 `_emergency_clear`

```python
def _emergency_clear(history: list, protect_recent_count: int, store, mode: str) -> dict:
    """截断时的应急清空：保留最近 N 条，上面全删，最旧那条改为"压缩失败"摘要。

    - history: 压缩历史消息列表（受保护消息已排除），按 idx 顺序排列
    - protect_recent_count: 保留最近条数（默认 10）
    - store: MessageStore，用于 delete_messages / update_message
    - 返回 {"status": "skipped", "mode": mode, "reason": "truncated, emergency cleared"}
    """
    if len(history) <= protect_recent_count:
        # 历史不足 10 条，无需清空，直接返回 skipped
        logger.warning(f"[Compact] history len {len(history)} <= {protect_recent_count}, no clear needed")
        return {"status": "skipped", "mode": mode, "reason": "truncated, no clear needed (too few)"}

    # 保留最近 protect_recent_count 条，上面的全删
    to_delete = history[:-protect_recent_count]
    delete_ids = [m.id for m in to_delete]

    # 最旧那条（保留区第一条，即 history[-protect_recent_count]）改为"压缩失败"摘要
    oldest_kept = history[-protect_recent_count]
    store.update_message(
        message_id=oldest_kept.id,
        content="[压缩失败，历史信息丢失] 上下文压缩时 LLM 输出截断，此条之上的历史已删除。可通过 journal.md 和知识图谱回溯。",
    )

    # 删除上面的消息
    store.delete_messages(session_id, delete_ids)

    logger.warning(f"[Compact] Emergency cleared: deleted {len(delete_ids)} msgs, kept recent {protect_recent_count}, marked oldest as lost-summary")
    return {"status": "skipped", "mode": mode, "reason": "truncated, emergency cleared"}
```

**应急清空逻辑说明**：

- 保留最近 10 条（用 `protect_recent` 机制，与正常压缩的受保护消息区分——应急清空的 10 条是"保留区"，不是"受保护消息"）
- 上面的全删（delete_messages 批量删）
- 最旧那条（保留区第一条）content 改为"[压缩失败，历史信息丢失]"摘要，update_message 写回 DB
- 返回 `{"status": "skipped", "mode": "sleep"/"force", "reason": "truncated, emergency cleared"}`
- 不调用 `/new`（用户功能），压缩函数内部完成清空

#### max_tokens 动态计算

`max_tokens`（LLM 单轮输出上限）**不读配置硬编码值**，由程序动态计算：

```python
def _read_max_output_tokens() -> int:
    """动态计算 max_output_tokens：contextWindowSize × 0.16，封顶 65536。"""
    context_window = _read_context_window_tokens()  # 如 200000
    val = int(context_window * 0.16)  # 200000 × 0.16 = 32000
    return min(val, 65536)  # 封顶 65536
```

**理由**：
- 换模型自动适配——不同模型 contextWindowSize 不同（如 128K/200K/256K），×0.16 自动算出对应 max_output_tokens
- 不依赖用户手设——用户不需要为每个模型调 `maxOutputTokens`
- 0.16 比例是通用保守值（ark-code-latest context 256K → 40960；Claude 200K → 32000）
- 封顶 65536 避免极端大窗口算出过大值（部分模型单轮输出硬限 64K）

`compressTargetTokens` 仍是配置值（60000），不动——压缩目标跟模型输出能力无关，是用户对"压缩后上下文大小"的期望。

#### max_tokens 传递

`max_tokens` 通过 `llm_config["litellm_kwargs"]` 注入，**不需要改 `call_subagent` / `_run_agent_loop` / `agent_runner_loop` 签名**。

`litellm_adapter.py:377-378` 的 `request_params.update(self.litellm_kwargs)` 会把 `litellm_kwargs` 里的所有键合并进 litellm 请求。`litellm_kwargs` 来自 `llm_config`（`agent/runner.py:285` 的 `cfg["litellm_kwargs"] = config.get("litellm_kwargs", {})`）。

所以 `call_subagent` 调用前，把 `max_tokens` 塞进 `llm_config["litellm_kwargs"]["max_tokens"]` 即可：

```python
# compat.py 模式二/三调用前
llm_config_with_max = dict(llm_config)
llm_config_with_max["litellm_kwargs"] = {
    **llm_config.get("litellm_kwargs", {}),
    "max_tokens": _read_max_output_tokens(),  # 动态算：contextWindowSize × 0.16，封顶 65536
}

result = call_subagent(
    agent_name="context-manager",
    task=prompt,
    llm_config=llm_config_with_max,  # 带 max_tokens
    history=history,
    ...
)
```

`call_subagent` 签名**不变**（不需要新增 `max_output_tokens` 参数），`_run_agent_loop` / `agent_runner_loop` / `client.chat` / `litellm_adapter.chat` 签名都不变。`max_tokens` 通过 `llm_config` → `litellm_kwargs` → `request_params` 自然到达 litellm。

**注意**：`config/user-config.json` 里的 `litellm_kwargs` 当前只有 `{"thinking":{"type":"enabled"}}`，不设 `max_tokens`（依赖平台默认 4K）。compat.py 调 context-manager 时动态注入 `max_tokens`（由 `_read_max_output_tokens` 动态算），不影响其他子 Agent。

### 4. 解析层：analysis 剥离

新增 `_strip_analysis` 辅助函数：

```python
def _strip_analysis(response: str) -> str:
    """剥离 <analysis>...</analysis> 块，只保留 keep/update/cursor 部分。"""
    import re
    # 先匹配闭合的 <analysis>...</analysis>
    cleaned = re.sub(r'<analysis>.*?</analysis>\s*', '', response, flags=re.DOTALL | re.IGNORECASE)
    # 再处理未闭合的 <analysis>（LLM 写了开始标签但没写结束）
    cleaned = re.sub(r'<analysis>.*$', '', cleaned, flags=re.DOTALL | re.IGNORECASE)
    return cleaned.strip()
```

解析流程：
```python
raw_response = call_subagent(...)
if raw_response == "COMPACT_TRUNCATED":
    # 截断应急清空（见上一节应急清空逻辑）
    return await _emergency_clear(history, protect_recent_count=10, store=store, session_id=session_id, mode=mode)
else:
    response = _strip_analysis(raw_response)
    # 原有解析逻辑：从 response 里提取 keep=/update=/cursor=
    keep_idxs = _parse_idx_list(...)
    update_list = _parse_update(...)
    cursor_idx = _parse_cursor(...)  # 模式三
```

### 5. 删除校验兜底

删除以下"自动修正"逻辑（`niu_api/compat.py` 模式二 L1946-1951 / 模式三 L2606-2611 等）：

- ~~update idx 不在 keep 时自动补进 keep~~ → 删
- ~~update idx 越界静默丢弃~~ → 删
- ~~update 摘要为空跳过~~ → 删
- ~~cursor idx 不在 keep 降级取 max~~ → 删

LLM 输出什么就用什么，靠 prompt 让它一次做对。

**删除后的健壮性保证**：

解析时保留最基本的 `idx in _idx_to_id` 映射检查（防止 LLM 幻觉 idx 导致 KeyError）：

```python
# 模式二 L1956-1958 现有逻辑保留（if idx in _idx_to_id 过滤）
updates = [{"message_id": _idx_to_id[idx], ...} for idx, content in update_list if idx in _idx_to_id]
# 新增：越界 idx 记 warning 日志（便于排查），不补救不重试
for idx, _ in update_list:
    if idx not in _idx_to_id:
        logger.warning(f"[Compact] LLM returned out-of-range update idx {idx}, silently dropped")
```

**设计取舍**：越界 idx 静默丢弃（不补救、不重试）。这是用户明确要求的——"不能给 LLM 留犯错后程序补救的后门"。如果 LLM 回了越界 idx，对应消息既不在 keep 也不在 update，会被当 delete 删掉。这虽然可能偏离 LLM 原意，但靠 prompt 的硬约束（update idx 必须在 keep 中）让 LLM 一次做对，不靠程序补救。记 warning 日志便于排查。

### 6. 术语清理（全项目）

| 改动 | 位置 |
|------|------|
| "事务"/"事务块" → 统一为"会话单元" | task prompt、system prompt |
| 删除 L0/L1/L2 相关说法 | 全项目所有残留处 |
| "远端/中端/近端" → "第一份（最早）/第二份（中间）/第三份（最近）" | task prompt、system prompt |
| 删除"按事务合并"措辞 | task prompt |

L0/L1/L2 残留清理范围（实施时 grep 全项目）：
- `config/agents/context-manager.md`
- `docs/feature-context-management.md`
- `docs/SYSTEM_MANUAL.md` 及子文档
- `AGENTS.md`
- 代码注释

**清理边界**：只清理"压缩相关的 L0/L1/L2 说法"（如"L0 摘要""L1 摘要""L2 原文""L2→L1→L0 删除优先级"）。**不动** LightRAG 知识库的 `l1`/`l2` 标签语义（那是知识图谱的层级标签，与压缩无关，保留）。实施时 grep 注意区分上下文。

### 7. 配置读取函数

`agent/subagent.py` 新增两个读取函数（与现有 `_read_warning_threshold` 等同模式）：

```python
def _read_compress_target_tokens() -> int:
    """读 compressTargetTokens，默认 60000。"""
    # 从 config/user-config.json 的 context.compressTargetTokens 读取

def _read_max_output_tokens() -> int:
    """动态计算 max_output_tokens：contextWindowSize × 0.16，封顶 65536。
    
    不读配置 maxOutputTokens（已删除硬编码）。
    换模型自动适配：不同模型 contextWindowSize 不同，×0.16 自动算对应值。
    """
    context_window = _read_context_window_tokens()  # 复用现有函数
    val = int(context_window * 0.16)
    return min(val, 65536)
```

**函数签名不变**（仍为 `() -> int`），但内部逻辑从"读配置 maxOutputTokens"改为"读 contextWindowSize × 0.16 封顶 65536"。

`niu_api/compat.py` 也需要读取这两个配置（模式二/三调用前用），从 `agent/subagent.py` 导入或复制实现（与现有 `_read_target_threshold` 的复用模式一致）。

### 8. `_compress_target` 处理

当前 `_compress_target`（compat.py L1827-1834）构造了完整指令但模式二没用、模式一用了。

**模式一说明**：模式一是"增量压缩"（`_is_mode2=False` 且非 force），在每轮对话后轻量压缩，逻辑与模式二/三的"全量压缩"不同。模式一目前用 `_compress_target` 构造压缩指令。

改造后：
- 模式二/三的 task prompt 直接内联方法论（不用 `_compress_target` 变量）
- 模式一**不改**（增量压缩逻辑独立，不在本次修复范围），`_compress_target` 保留给模式一用，不算死代码
- 如果模式一未来也要按新方法论走，单独开任务处理，不在本次范围内

明确保留 `_compress_target` 给模式一，避免实施者误删。

## 不改动的部分

- `_build_compress_history` 函数（history 列表构造，已验证正确）
- `call_subagent` / `_run_agent_loop` / `agent_runner_loop` 签名（`max_tokens` 通过 `llm_config["litellm_kwargs"]` 注入，不改签名）
- keep/update/cursor 三行输出格式（解析逻辑不大改，只加 analysis 剥离）
- 压缩执行逻辑（DB 删除/更新/级联清理不变）
- system prompt 的有效规则（会话单元、摘要格式、禁止无限衰减等保留，只清理 L0/L1/L2 和术语）
- 模式三 dream 安全边界 / force_protect_recent / chat_lock_already_held
- history 列表传递机制（模式二/三的 history 改造已验证正确）

## 风险与验证

### 风险点

1. **截断时应急清空丢历史**：LLM 输出截断时不重试，直接应急清空（保留最近 10 条，上面全删，最旧那条标"压缩失败"）。靠 journal.md + 知识图谱（entity-extractor / dream-evolver / journal-agent 三层前置兜底）让主 Agent 读回历史。用户已改 niu.md 让主 Agent 读 journal.md 自我修复旧记忆。
2. **LLM 不严格按格式输出**：analysis 标签缺失/未闭合/大小写。`_strip_analysis` 正则兜底三种情况。
3. **模式一兼容性**：`_compress_target` 改动可能影响模式一。实施时评估，必要时保留给模式一。
4. **删除校验兜底后 LLM 出错无补救**：靠 prompt 方法论 + analysis 草稿块让 LLM 一次做对。这是设计取舍——不要"程序补救削弱 prompt 约束"。

### 验证方式

1. **单元测试**：`_strip_analysis` 各种格式（闭合/未闭合/大小写/缺失）
2. **集成测试**：mock LLM 返回含 analysis 的完整回复，验证解析正确
3. **应急清空测试**：mock LLM 返回 `finish_reason=length`（call_subagent 返回 `"COMPACT_TRUNCATED"`），验证应急清空触发——最近 10 条保留 + 上面全删 + 最旧那条改"压缩失败"摘要
4. **端到端验证**：真实触发模式二/三压缩（如之前 316 条 history 场景），检查压缩后 keep/update 质量和 token 达标情况

## 实施顺序建议

1. 配置层变更（`config/user-config.json` 删 `targetThreshold` 加 `compressTargetTokens`；不配 `maxOutputTokens`，程序动态算）+ 读取函数 `read_compress_target_tokens` / `read_max_output_tokens`（后者动态算）
2. 新增 `_strip_analysis` 辅助函数 + 单元测试
3. finish_reason 传递链改造（`MockResponse` 加字段 + `litellm_adapter` 流式捕获 + `agent_loop` return_value + `call_subagent` 检测）
4. 新增 `_emergency_clear` 应急清空函数 + 单元测试
5. 模式二 task prompt 重写 + 单次 call_subagent + 截断应急清空（llm_config 动态注入 max_tokens）
6. 模式三 task prompt 重写 + 单次 call_subagent + 截断应急清空
7. 删除校验兜底逻辑
8. 术语清理（L0/L1/L2 + 事务→会话单元 + 远端中端近端→三份）
9. `_compress_target` 评估处理
10. 端到端验证
