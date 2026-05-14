# 同步/异步分层整改方案 v3

**Goal:** 修复 3 个确定的 bug，做 2 个零风险重构。不改变任何工作流。

**铁律:** 每改一处，启动程序验证，确认没问题再改下一处。

---

## 调查结论

### 当前系统的工作机制（基线 65ff2392，能正常工作）

1. **agent_runner_loop 是同步生成器**，用 `yield` 输出文本块，用 `yield from` 透传 handler.dispatch 的 yield。这是核心架构，不能动。

2. **消息历史是"三重丢弃"策略**：
   - 不写入：add_message 只存 role + content，不存 tool_calls/tool_results
   - 不加载：load_history 只返回 {role, content}
   - 不注入：agent_runner_loop 只处理 role in (user, assistant) 且 content 非空
   - 结果：LLM 每次看到的是"纯净对话摘要"，看不到工具调用历史。**这是当前能正常工作的原因。**

3. **do_no_tool 返回 MockResponse 对象**，在 agent_loop.py 中 `str(outcome.data)` 输出 `<MockResponse ...>` repr 字符串。这导致：
   - tool_result 的 content 是 MockResponse 的 repr 字符串（不是实际回复内容）
   - 但 no_tool 正常路径下 next_prompt="" 会 break，tool_results 不会被添加到 messages
   - **实际影响**：runner.py 的 return_value 处理中会从 MockResponse.content 提取文本，所以最终输出是正确的
   - **但**：如果 no_tool 走异常路径（空白响应等），MockResponse repr 字符串会泄露

4. **no_tool 异常路径产生空 tool_call_id**：当 LLM 返回空白/反常号等异常内容时，no_tool 的 next_prompt 不为空，不会 break，tool_results 会添加 `{"tool_use_id": "", "content": "..."}` 的条目，产生孤立的 tool 消息。

5. **handler.py 有 2 处完全相同的 asyncio 桥接代码**，用于处理 MCP 工具函数返回 coroutine 的情况。代码逻辑正确，只是重复。

6. **memory-server 有 9 个伪 async handler**，函数体内无 await，是伪 async。

7. **hasattr(ret, 'status') 永远返回 False**：handler.py tool_after_callback 中用 `hasattr(ret, 'status')` 检查 dict 是否有 status 键，但 dict 的属性访问不等于键访问，所以 Interaction Habits 置信度永远不更新。

8. **空白行bug根因**：每次工具调用后对话框多一条空白行。根因是 runner.py 清理正则不完整：
   - 缺少 `\*\*LLM Running.*?\*\*\s*` 清理（agent_loop.py L146 yield 的标记）
   - 缺少 `🛠️.*?````.*?````\s*` 清理（agent_loop.py L209 yield 的工具调用标记）
   - 缺少 ````````\s*` 清理（agent_loop.py L213/L215 yield 的代码块标记）
   - LLM 返回的 content 中包含裸换行（`\n\n`），清理后残留空行

---

## 方案：5 个独立改动，按风险从低到高排序

### 改动 1: handler.py 收拢 asyncio 桥接代码（零风险重构）

**性质**：纯重构，不改变任何行为

**改动**：
- 在 handler.py 顶部添加 `_run_coroutine(coro)` 辅助函数
- 将 2 处散落的 12 行 asyncio 桥接代码替换为 `result = _run_coroutine(result)`

**验证**：启动程序，发送一条消息，确认正常回复

---

### 改动 2: memory-server 9 个伪 async handler 改为同步 def（零风险重构）

**性质**：纯重构，不改变任何行为

**改动**：
- 将 9 个 `async def xxx_handler(...)` 改为 `def xxx_handler(...)`
- call_tool dispatcher 中去掉 await（call_tool 本身保持 async def，MCP SDK 要求）
- **注意**：模块级别名函数（L725-732 的 `user_memory_remember`, `user_memory_forget`, `user_memory_list`）如果引用了 handler，也需要同步调整

**验证**：启动程序，调用记忆工具（如"记住我喜欢Python"），确认正常

---

### 改动 3: 修复 no_tool 异常路径产生空 tool_call_id（bug 修复）

**性质**：bug 修复，只影响异常路径

**改动**：在 agent_loop.py 中，只有 tid 非空时才添加 tool_result：
```python
# 旧：
if outcome.data is not None:
    ...
    tool_results.append(...)
else:
    tool_results.append(...)

# 新：
if tid:
    if outcome.data is not None:
        ...
        tool_results.append(...)
    else:
        tool_results.append(...)
```

**效果**：no_tool 场景下 tid="" 不会产生 tool_result，也不会产生 tool 消息。这同时消除了 MockResponse repr 字符串泄露到 tool_result 的可能性（因为 no_tool 的 tool_result 根本不会被构造）。

**验证**：启动程序，发送一条消息，确认正常回复。检查日志无 API 400 错误。

---

### 改动 4: 修复 hasattr(ret, 'status') 永远返回 False（bug 修复）

**性质**：bug 修复，只影响 Interaction Habits 置信度更新

**改动**：在 handler.py tool_after_callback 中，将 `hasattr(ret, 'status')` 改为 `isinstance(ret, dict) and 'status' in ret`：
```python
# 旧：
if hasattr(ret, 'status') and ret.status == 'success':
    ...

# 新：
if isinstance(ret, dict) and ret.get('status') == 'success':
    ...
```

**效果**：Interaction Habits 的置信度更新逻辑将正常执行。

**验证**：启动程序，连续调用同一工具多次，检查日志中是否有置信度更新记录。

---

### 改动 5: 修复空白行bug — runner.py 清理正则补全（bug 修复）

**性质**：bug 修复，只影响流式输出的清理

**改动**：在 runner.py 的清理逻辑中，补全缺失的正则模式：
```python
# 新增清理模式：
re.sub(r'\*\*LLM Running.*?\*\*\s*', '', text)        # 清理 LLM Running 标记
re.sub(r'🛠️.*?````.*?````\s*', '', text, flags=re.DOTALL)  # 清理工具调用标记
re.sub(r'`````\s*', '', text)                           # 清理代码块标记
re.sub(r'\n{3,}', '\n\n', text)                         # 多余空行压缩为双换行
```

**效果**：工具调用后不再出现多余空白行。

**验证**：启动程序，调用工具，确认对话框不再出现空白行。

---

## 明确不做的事

| 不做 | 原因 |
|------|------|
| session.py 添加同步包装方法 | agent 核心不直接调用 session.py，不需要同步接口 |
| context_manager.py 改为同步 | 它被 async 路由调用，改为同步需要 _run_async 桥接，可能在事件循环中死锁 |
| tool_result 持久化 | 会改变消息历史构成，导致 Agent 看到工具调用记录而行为异常。这是"三重丢弃"策略的核心，不能轻易打破 |
| agent_loop.py 加 on_tool_result 回调 | 在生成器协议中插入回调，可能干扰 yield/send 通道 |
| history 注入支持 tool 消息 | 同 tool_result 持久化，会改变 Agent 看到的上下文 |
| session.py 扩展 tool_call_id 字段 | 当前不需要，tool 消息不被持久化 |
| do_no_tool 返回值改为 response.content | runner.py 依赖 MockResponse 对象来提取 thinking 和 content，改返回值会破坏 runner.py |
| MockResponse 防御性提取 | 改动3的 `if tid:` 守卫已使 no_tool 的 tool_result 不被构造，防御性提取是死代码 |

---

## 执行流程

每个改动按 TDD 模式执行：
1. 写测试（如果可测试）
2. 运行测试确认失败
3. 改代码
4. 运行测试确认通过
5. 启动程序实际验证
6. 确认没问题再提交
7. 做下一个改动
