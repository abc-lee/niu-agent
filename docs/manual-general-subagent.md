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
- 提示词正文编写规则（角色职责 / 工作流程 / 输出格式 / @niu content 拦截层使用时机 / 何时终止）
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
   - **重要**：如果 `allowAsync: true`，正文必须写明 @niu content 拦截层的使用时机（如"遇到用户意图不明确时用 @niu content 询问，不要自行假设"），否则子 Agent 不会主动询问
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
- 子 Agent 主动询问主 Agent（@niu content 拦截层，仅异步子 Agent 自动启用）
- 主 Agent 查询子 Agent 进度（`check_subagent_progress` 工具）
- 异步子 Agent 完成汇报（push 到 MainAgentRequestQueue）
- 5 个死锁约束（cancel_pending_ask / _ask_terminated 标记 / request_stop_all_subagents / route_message / 超时决策）

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
