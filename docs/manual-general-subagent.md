# 通用子 Agent 分册

> 阶段三实现的通用子 Agent 体系。主 Agent 可通过参考模板自定义新子 Agent 配置（MD 文件），动态加载，由主 Agent 同步或异步调用完成长时复杂任务。

## 一、设计目标

- **减少主 Agent 上下文占用**：大段工作丢给子 Agent，主 Agent 上下文留给决策和协调
- **支持长时任务**：异步调用不阻塞主 Agent
- **支持专业性任务**：用户提供专业提示词或专业文档，交给专门的子 Agent 处理

## 二、模板位置

`config/agent-template.md`——子 Agent 配置模板，含所有可用 MCP 服务器清单和 frontmatter 字段说明。模板本身不被加载，仅供主 Agent 参考编写。

模板包含：
- frontmatter 字段（name / description / mode / temperature / taskDescription / permissions / mcpServers / mcpToolFilter / disableBaseTools / allowBaseTools / allowAsync）
- 提示词正文编写规则（角色职责 / 工作流程 / 输出格式 / @niu-agent content 拦截层使用时机 / 何时终止）
- 可用 MCP 服务器清单（必需 + 可选）
- 字段格式示例

## 三、配置目录

| 目录 | 用途 | 示例 |
|------|------|------|
| `config/agents/` | 专用子 Agent（项目内置，启动加载） | `file-processor.md`、`niu.md` |
| `~/.niu/agents/` | 通用子 Agent（主 Agent 运行时创建，动态加载） | `photo-organizer.md`、`doc-summarizer.md` |

**同名优先级**：专用子 Agent 优先（`config/agents/` 先查）。

## 四、动态加载机制

程序在 `chat()` 入口（每次对话开始时）调用 `_refresh_base_tools_schema_if_dirty()`：

1. 扫描 `~/.niu/agents/` 目录
2. 与 `NiuRunner._known_user_subagents` 集合对比
3. 发现新 MD 文件 → 重算 `base_tools_schema` → 新子 Agent 的 `chat-with-{name}` 工具自动出现
4. 无变化时不重算（保持对象引用稳定，避免无谓拷贝）

**特点**：
- 不用 watchdog / 定时器，复用现有动态组装机制
- 主 Agent 写完 MD 后下一轮对话开始时工具才出现（自然时序）
- 重算返回完整 base 集（基础工具 + MCP 工具 + 所有 chat-with-* + check_subagent_progress）

## 五、跳过条件（方式 B：不允许坏工具）

以下情况的 MD 文件会被跳过，不生成对应工具：

- 文件名非 kebab-case（含空格 / 大写 / 中文等，正则 `^[a-z0-9]+(-[a-z0-9]+)*$`）
- MD 文件不存在
- frontmatter 为空或 YAML 解析失败
- `description` 字段缺失或为空
- `_resolve_agent_md_path` 对 `agent_name` 做 kebab-case 校验，防御路径穿越（如 `../`）

跳过时会 log warning，不阻塞其他子 Agent 加载。

## 六、MCP 工具映射

子 Agent 的 MCP 工具由 frontmatter `mcpServers` 字段指定：

```yaml
mcpServers:
  - photo-server
  - lightrag-server
```

加载时从已加载的全局 ToolRegistry 过滤，无需额外加载逻辑。

**未加载服务器的处理**：如果 `mcpServers` 含未加载的服务器（不在 `mcp_loader.REQUIRED_SERVERS` + `OPTIONAL_SERVERS` 里），对应工具缺失但不阻塞，log warning 提示。

**mcpToolFilter 白名单**：可选，按 server 分组的 map，进一步限制具体工具：

```yaml
mcpToolFilter:
  lightrag-server:
    - lightrag_insert
    - lightrag_search_entities
```

## 七、主 Agent 创建子 Agent 流程

1. 主 Agent 读 `config/agent-template.md` 了解字段和可用 MCP 服务器
2. 主 Agent 用基础工具（读写文档）写新 MD 到 `~/.niu/agents/{name}.md`：
   - name 用 kebab-case（如 `photo-organizer`、`doc-summarizer`）
   - frontmatter 填 description / mcpServers / allowAsync 等（description 必填）
   - 正文写系统提示词
   - **重要**：如果 `allowAsync: true`，正文必须写明 @niu-agent content 拦截层的使用时机（如"遇到用户意图不明确时用 @niu-agent content 询问，不要自行假设"），否则子 Agent 不会主动询问
3. 主 Agent 当前任务结束
4. 下一轮 `chat()` 入口扫描发现新 MD → 重算 schema → `chat-with-{name}` 工具出现
5. 主 Agent 调用 `chat-with-{name}`（同步或异步）执行任务

## 八、同步 vs 异步调用

| 模式 | 行为 | 适用场景 |
|------|------|----------|
| 同步 | 主 Agent 阻塞等子 Agent 跑完拿结果 | 短时任务 |
| 异步 | 立即返回"已开始异步工作"，子 Agent 后台跑 | 长时任务 |

**异步调用条件**：
- 子 Agent frontmatter `allowAsync: true`
- 主 Agent 调用时 `async_mode: true`

**异步子 Agent 完成汇报**：
- 子 Agent 完成后自动 push 完成通知到 `MainAgentRequestQueue` 内存队列
- db_monitor 链路 A 检测主 Agent 空闲 → 推 SSE → 前端调 /api/chat/session → 主 Agent 新一轮 LLM
- 主 Agent 拿结果判断下一步（继续 / 向用户汇报）

## 九、与阶段一+二的衔接

通用子 Agent 完整复用阶段一+二的全部交互能力：

### 阶段一能力（主子 Agent 通信通道）
- 主 Agent 通过 @子名 给子 Agent 发消息
- /stop 终止子 Agent（子 Agent LLM 生成总结再退出）
- 双击停止按钮触发批量 /stop

### 阶段二能力（异步交互 + ask）
- 子 Agent 主动询问主 Agent（@niu-agent content 拦截层，仅异步子 Agent 自动启用）
- 主 Agent 查询子 Agent 进度（`check_subagent_progress` 工具）
- 异步子 Agent 完成汇报（push 到 MainAgentRequestQueue）
- 5 个死锁约束（cancel_pending_ask / _ask_terminated 标记 / request_stop_all_subagents / route_message / 超时决策）

## 九点五、同步子 Agent @niu-agent/@end 交互

所有子 Agent（同步 + 异步）都被程序注入 @niu-agent/@end 守则：
- 询问主 Agent：content 中包含 `@niu-agent`
- 结束会话：content 中包含 `@end`

同步子 Agent 调用时主 Agent 在工具循环阻塞等待，子 Agent @niu-agent 问题会被包装成 `[子名] 问题` 作为工具返回值送给主 Agent。主 Agent 看到 JSON 工具结果后调 chat-with-xxx(task="", answer="@子名 回答", unique_name="子名") 回复。

程序触发子 Agent（无主 Agent 在线）由 helper 自动回复固定文案。

## 十、维护注意事项

- **MCP 服务器清单变化**：新增/移除 MCP 服务器时，同步更新 `config/agent-template.md` 的"可用 MCP 服务器"段
- **REQUIRED_SERVERS 改动**：`mcp_loader.REQUIRED_SERVERS` 改动会影响子 Agent 可用工具，需检查现有通用子 Agent 的 `mcpServers` 字段是否仍有效
- **用户清理 ~/.niu/agents/**：下一轮 `chat()` 入口扫描会自动移除对应工具（集合 diff 检测到文件消失 → 重算 schema）
- **坏 MD 排查**：日志含 `Sub-agent 'xxx' has empty/invalid frontmatter, skip` 等警告，按警告修正 MD 即可

## 十一、相关文件

| 文件 | 责任 |
|------|------|
| `config/agent-template.md` | 子 Agent 配置模板 |
| `config/agents/` | 专用子 Agent 目录（项目内置） |
| `~/.niu/agents/` | 通用子 Agent 目录（用户动态创建） |
| `agent/subagent.py` | `_resolve_agent_md_path` / `get_subagent_config` / `get_subagent_prompt` |
| `agent/runner.py` | `get_tools_schema` / `_refresh_base_tools_schema_if_dirty` / `_KEBAB_CASE_RE` |
| `config/agents/niu.md` | 主 Agent 提示词（含通用子 Agent 说明段） |

## 十二、相关文档

- 阶段一+二设计：`docs/superpowers/specs/2026-07-02-main-subagent-interaction-design.md`
- 阶段三设计：`docs/superpowers/specs/2026-07-04-general-subagent-stage3-design.md`
- 阶段三实施计划：`docs/superpowers/plans/2026-07-04-general-subagent-stage3.md`


## 十三、子 Agent 标签页与事件通道

> 动态子 Agent 标签页机制：主 Agent SSE 流推送 `subagent_started` 事件 → 前端自动创建 tab → tab 建立独立 SSE 连接实时展示子 Agent 的回复、工具状态、思考链和提问。子 Agent 可通过 `@user` 向用户提问，用户在 tab 内回答，实现子 Agent 与用户的直接双向交流。

### 1. 子 Agent 标签页（动态 Tab）

每个子 Agent 启动时，主 Agent SSE 流（`/api/events/stream`）推送 `subagent_started` 事件，前端收到后自动创建一个独立的 tab：

- **tab 创建**：`subagent_started` 事件携带 `unique_name` / `agent_name` / `agent_type` 等字段，前端 `SubagentSSEManager` 创建 tab 并建立独立 SSE 连接 `GET /api/subagents/{unique_name}/stream`
- **事件展示**：每个 tab 通过独立 SSE 连接接收该子 Agent 的 `reply` / `tool_status` / `thinking_chain` / `question` 事件，实时渲染子 Agent 的工作过程
- **关闭方式**：tab 不可手动关闭，唯一关闭方式是子 Agent 输出 `@end`（子 Agent 正常结束）或异常终止（推送 `subagent_error`）
- **tab 视觉状态**：
  - **活跃**（当前选中的 tab，高亮边框）
  - **非活跃**（后台 tab，灰色）
  - **有未读**（tab 标题旁显示红点 badge）
  - **等待回答**（`@user` 提问时 tab 闪烁脉动 + 红点 badge）
  - **@end 完成**（tab 标记完成状态，灰色 + 勾选图标）
  - **异常终止**（tab 标记错误状态，红色边框）
- **3D 悬浮效果**：active tab 上浮 2px + 双层阴影（`box-shadow`），hover 时上浮 1px，营造层级感

事件通道由独立的 `SubagentEventBus`（`niu_api/internal/subagent_event_bus.py`）管理，与主 Agent 的消息推送通道分离。

### 2. @user 提问机制

子 Agent 可通过 `@user` 前缀向用户提问，实现子 Agent 与用户的直接双向交流：

- **拦截识别**：子 Agent 输出 `@user 问题描述` 时，拦截层（`INTERCEPTED_ASK_USER`）识别后推送 `question` 事件到 `SubagentEventBus`
- **前端展示**：对应 tab 高亮显示问题（黄色卡片 + tab 闪烁脉动 + 红点 badge），提示用户需要回答
- **用户回答**：用户在 tab 内输入回答，通过 `POST /api/subagents/{unique_name}/message` 提交，子 Agent 从阻塞中恢复继续工作
- **超时机制**：子 Agent 阻塞等待回答，默认 600s 超时
- **同步/异步均支持**：同步子 Agent（主 Agent 阻塞等待）和异步子 Agent（后台运行）均支持 `@user` 提问
- **实现**：`AskUserFuture` + `UserAskRegistry`（`agent/ask_user.py`），通过 future 阻塞子 Agent 线程，用户回答后 resolve future 唤醒

### 3. @end 优先级规则

子 Agent 输出中同时包含 `@end` 和 `@niu-agent` / `@user` 时，`@end` 优先级最高，直接退出，不处理提问：

- **规则**：`@end` 检查在最前面，一旦匹配立即终止子 Agent 循环
- **原因**：已结束的子 Agent 再处理提问无意义，避免无效交互
- **实现**：`agent/generic/agent_loop.py` `_intercept_at_prefix_content` 中 `@end` 检查位于 `@niu-agent` / `@user` 之前

### 4. 同步子 Agent SSE 404 竞态修复

同步子 Agent 启动时存在 SSE 404 竞态问题，已修复：

- **根因**：同步路径中 `subagent_started` 的 `call_soon_threadsafe` 入队在 `pre_register`（创建 ring buffer）之前。主 loop 按 FIFO 先广播 `subagent_started` 事件 → 前端立即连 SSE → `has_subagent()` 检查 `_ring_buffers` → 还没创建 → 返回 False → 404
- **修复**：`pre_register` 提前到 `subagent_started` 推送之前调用，保证主 loop FIFO 先创建 ring buffer 再广播事件
- **`if not answer` 守卫**：恢复路径（主 Agent 回答子 Agent 提问后继续）跳过冗余的 `pre_register` + `subagent_started`
- **finally 条件 close**：`finally` 块检查 `state != 'waiting_for_answer'` 才 close，避免子 Agent 等待回答时被误清理
- **`is_closing` 去重**：`is_closing()` 函数检查 `_close_epochs` 字典，防止场景 1/8（正常完成）双重 close
- **except 块 `subagent_error` 推送**：子 Agent 异常终止时推送 `subagent_error` 事件，前端 tab 显示红色错误状态

### 5. SubagentEventBus 独立事件总线

`SubagentEventBus`（`niu_api/internal/subagent_event_bus.py`）是子 Agent 专用的事件通道，与主 Agent 的消息推送分离：

- **per-unique_name 事件队列路由**：`_subscribers` dict 按 `unique_name` 路由事件，每个子 Agent 的事件独立推送
- **ring buffer 断线重连补发**：每个子 Agent 维护 `deque(maxlen=100)` ring buffer，SSE 断线重连时补发最近 100 条事件
- **epoch 机制防止同名子 Agent 误删**：`_close_epochs` + `_epoch_lock`，防止 5 分钟延迟清理窗口内同名子 Agent 重启被误删
- **核心方法**：
  - `pre_register(unique_name)`：提前创建 ring buffer（在 `subagent_started` 之前调用，消除竞态）
  - `subscribe(unique_name)`：订阅子 Agent 事件流，返回 asyncio.Queue
  - `notify_subagent_event_sync(unique_name, event_type, data)`：同步推送事件（从 executor 线程调用）
  - `close(unique_name)`：关闭事件流，触发延迟清理
  - `has_subagent(unique_name)`：检查子 Agent 是否存在（ring buffer 是否已创建）
  - `is_closing(unique_name)`：检查子 Agent 是否已 close（区分"已 close 过"和"从未 close"）

### 6. 事件类型清单

子 Agent 事件分为两类通道：**主 Agent SSE 流**（`/api/events/stream`）和**子 Agent 独立 SSE 流**（`/api/subagents/{unique_name}/stream`）。

| 事件类型 | 推送通道 | 说明 |
|----------|----------|------|
| `subagent_started` | 主 SSE 流 `/api/events/stream` | 子 Agent 启动通知，前端创建 tab + 建立独立 SSE |
| `reply` | 子 Agent SSE `/api/subagents/{id}/stream` | 子 Agent 回复文本，Markdown 渲染 |
| `tool_status` | 子 Agent SSE | 工具调用状态（工具名 + start/end + 摘要） |
| `thinking_chain` | 子 Agent SSE | 子 Agent 思考过程，折叠/展开（>200px 才显示按钮） |
| `question` | 子 Agent SSE | `@user` 提问，黄色高亮卡片 |
| `subagent_suspended` | 子 Agent SSE | 子 Agent 等待主 Agent 回答 |
| `subagent_error` | 子 Agent SSE | 子 Agent 异常终止 |
| `subagent_closed` | 子 Agent SSE | 子 Agent 已结束，tab 标记完成并关闭 |

### 7. API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/subagents/{unique_name}/stream` | GET | 子 Agent 独立 SSE 端点 |
| `/api/subagents/running` | GET | 在跑子 Agent 列表（窗口恢复时用） |
| `/api/subagents/{unique_name}/message` | POST | 用户向子 Agent 发消息/回答 `@user` 提问 |
| `/api/stop_all` | POST | 停止所有子 Agent |

### 相关文件

| 文件 | 责任 |
|------|------|
| `niu_api/internal/subagent_event_bus.py` | SubagentEventBus（pre_register/subscribe/close/has_subagent/is_closing/notify_subagent_event_sync） |
| `niu_api/chat.py` | 子 Agent SSE 端点 + POST API（L720-749） |
| `agent/handler.py` | 同步路径 pre_register + subagent_started + finally close + except subagent_error（L1067-1195） |
| `agent/subagent.py` | `_run_agent_loop`（last_reply）+ `_run_subagent_async`（L262-296, L1294-1386） |
| `agent/ask_user.py` | AskUserFuture + UserAskRegistry |
| `agent/route_to_subagent.py` | route_to_subagent 公共函数 |
| `agent/generic/agent_loop.py` | @end 优先级 + @niu-agent + @user 拦截（L149-215） |
| `ui/main/windows/assistant/chat.html` | tab CSS + thinking CSS（L749-879）+ 子 Agent tab JS（L2640-3091） |
| `ui/main/main.js` | SubagentSSEManager + subagent_started 处理（L1809-1965） |
| `ui/main/preload-chat.js` | 5 个新增 IPC 接口 |

### 相关文档

- 动态子 Agent 标签页设计：`docs/superpowers/specs/2026-08-03-dynamic-subagent-tabs-design.md`
- 子 Agent 标签页前端 UI 设计：`docs/superpowers/specs/2026-08-04-subagent-tabs-frontend-ui-design.md`