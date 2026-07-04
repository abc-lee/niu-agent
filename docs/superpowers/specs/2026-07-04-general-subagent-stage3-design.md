# 通用子 Agent 设计（阶段三）Design Spec

**日期**：2026-07-04
**阶段**：阶段三（通用子 Agent）
**前置**：阶段一（主子 Agent 通信通道）+ 阶段二（异步调用 + ask_main_agent + 内存队列）已完成

## 目标

实现"通用子 Agent"——主 Agent 通过参考模板，自定义一个新的子 Agent 配置（MD 文件），动态加入到可用子 Agent 列表，由主 Agent 同步或异步调用它完成长时、复杂、专业性任务，以减少主 Agent 自己上下文的占用。

## 核心设计原则

1. **MD 文件就是配置源**——不引入 db / json 等其他配置存储，复用现有 `config/agents/` MD 加载机制
2. **静态配置 vs 动态配置分离**——专用子 Agent（项目内置）放 `config/agents/`，通用子 Agent（主 Agent 运行时创建）放 `~/.niu/agents/`
3. **复用现有加载入口**——`get_subagent_config` / `get_tools_schema` 改造而非重写
4. **每次组装上下文时扫描**——不用 watchdog / 定时器，在 `chat()` 入口扫一次 `~/.niu/agents/`
5. **MCP 工具无需额外加载**——子 Agent 的 MCP 工具由 frontmatter `mcpServers` 字段指定，从已加载的 ToolRegistry 过滤即可
6. **主 Agent 用现有基础工具创建子 Agent**——主 Agent 已具备读写文档的能力，不需要新增"创建子 Agent"的专用 MCP 工具

## 架构总览

```
config/agent-template.md      ← 模板（项目内置，参考用，不加载）
config/agents/                ← 专用子 Agent（项目内置，启动加载）
~/.niu/agents/                ← 通用子 Agent（主 Agent 运行时创建，动态加载）

主 Agent 流程：
1. 读 config/agent-template.md 了解字段和规则
2. 写新 MD 到 ~/.niu/agents/{name}.md
3. 当前任务结束
4. 下一轮 chat() 入口 → _refresh_base_tools_schema_if_dirty() 扫 ~/.niu/agents/
5. 新子 Agent 出现在 chat-with-{name} 工具列表
6. 主 Agent 调用 chat-with-{name}（同步 or 异步）
7. 异步子 Agent 跑完后 push 完成汇报 → 主 Agent 新一轮 LLM 处理
```

## 组件设计

### 组件 1：模板文件 `config/agent-template.md`

**职责**：供主 Agent 参考的子 Agent 配置编写指南。

**内容结构**：

```markdown
---
# 子 Agent 配置模板
# 复制此文件到 ~/.niu/agents/{name}.md 并填写以下字段

description: ""        # 一句话描述子 Agent 的职责（必填，主 Agent 据此判断何时调用）
mcpServers: []         # 该子 Agent 可用的 MCP 服务器列表（见下方"可用 MCP 服务器"）
mcpToolFilter: null    # 可选：白名单过滤特定工具（如只用 photo-server 的 search_photos）
allowAsync: false      # 是否允许异步调用（长时任务设为 true）
permissions: []        # 权限声明（保留字段）
taskDescription: ""    # 任务描述模板（主 Agent 调用时填的入参说明）
disableBaseTools: false # 是否禁用基础工具
---

# 提示词正文

在此编写子 Agent 的系统提示词。要说清楚：
- 子 Agent 的角色和职责边界
- 工作流程（先做什么、再做什么）
- 输出格式要求
- 何时主动询问主 Agent（异步模式下用 ask_main_agent）
- 何时该终止自己
```

**附：可用 MCP 服务器清单**

模板正文要列出所有已加载的 MCP 服务器及其工具，供主 Agent 选择 `mcpServers` 字段：

- `file-parser` — 文档解析（PDF/Word/PPT/Excel/MD/HTML）
- `lightrag-server` — 知识图谱 + 向量检索
- `photo-server` — 照片管理 + 人脸识别
- `config-manager` — 配置管理
- `memory-server` — 用户长期记忆
- `session-manager` — 会话管理
- `browser-server` — 浏览器自动化
- `brain-region-server` — 脑区状态管理
- `scheduler-server` — 定时任务调度

（具体清单从 `mcp_loader.REQUIRED_SERVERS` + `OPTIONAL_SERVERS` 自动生成，避免模板和实际加载脱节）

### 组件 2：加载层改造

**改造点 1**：`agent/subagent.py` 多目录查找

新增 `_resolve_agent_md_path(name)` helper，统一两个调用点（`get_subagent_config` L297 + `get_subagent_prompt` L328）：

```python
def _resolve_agent_md_path(name: str) -> str | None:
    """先查 config/agents/{name}.md，再查 ~/.niu/agents/{name}.md"""
    project_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "agents", f"{name}.md")
    if os.path.exists(project_path):
        return project_path
    user_path = os.path.join(os.path.expanduser("~/.niu/agents"), f"{name}.md")
    if os.path.exists(user_path):
        return user_path
    return None
```

`get_subagent_config` / `get_subagent_prompt` 改为调此 helper，找不到时抛原有异常。

**改造点 2**：`agent/runner.py` `get_tools_schema()` 扫描 `~/.niu/agents/`

L253-262 现有逻辑只读 `niu.md` 的 `sub agents` 字段。改为：

```python
# 1. 从 niu.md 读专用子 Agent 名单
sub_agents_list = niu_config.get("sub agents", [])

# 2. 扫描 ~/.niu/agents/*.md 加通用子 Agent 名单
user_agents_dir = os.path.expanduser("~/.niu/agents")
if os.path.isdir(user_agents_dir):
    for f in os.listdir(user_agents_dir):
        if f.endswith(".md") and not f.startswith("_"):
            user_agents_list.append(os.path.splitext(f)[0])

# 3. 合并去重，为每个名字生成 chat-with-{name} schema
all_subagents = list(dict.fromkeys(sub_agents_list + user_agents_list))  # 保序去重
```

**改造点 3**：`agent/runner.py` `chat()` 入口加刷新

在 `chat()` 方法 L1955 `tools_schema = self.base_tools_schema.copy()` **之前**加：

```python
def _refresh_base_tools_schema_if_dirty(self):
    """每次对话开始时扫 ~/.niu/agents/，发现新 MD 就重算 base_tools_schema"""
    user_agents_dir = os.path.expanduser("~/.niu/agents")
    if not os.path.isdir(user_agents_dir):
        return
    current_files = {f for f in os.listdir(user_agents_dir) if f.endswith(".md") and not f.startswith("_")}
    if current_files != self._known_user_subagents:
        self._known_user_subagents = current_files
        self.base_tools_schema = get_tools_schema()  # 重算

def chat(self, ...):
    self._refresh_base_tools_schema_if_dirty()
    tools_schema = self.base_tools_schema.copy()
    ...
```

`NiuRunner.__init__` 初始化 `self._known_user_subagents = set()`。

**时序说明**：主 Agent 写完新 MD 后，当前轮看不到对应工具（`chat()` 入口已扫过）。下一轮对话开始（用户发新消息，或完成汇报触发新一轮）时扫到新 MD，工具出现。这是自然时序，不需要特殊处理。

### 组件 3：主 Agent 提示词 `config/agents/niu.md` 更新

在 niu.md 提示词正文加一段"通用子 Agent"说明：

```markdown
## 通用子 Agent

对于复杂、长时、耗时或专业性的任务，你可以创建专用的子 Agent 来处理，以减少自己上下文的占用。

### 模板位置

子 Agent 配置模板在 `config/agent-template.md`，包含所有可用 MCP 服务器清单和 frontmatter 字段说明。

### 何时创建子 Agent

- **复杂任务**：多步骤、需要长期跟踪的任务（如"整理我所有照片里的人物"）
- **耗时任务**：单个操作很慢（如批量处理几百个文件），异步调用避免阻塞你
- **专业性任务**：用户提供专业提示词或专业文档，交给专门的子 Agent 处理
- **减少上下文占用**：大段工作丢给子 Agent，你的上下文留给决策和协调

### 如何创建子 Agent

1. 读 `config/agent-template.md` 了解字段和可用 MCP 服务器
2. 用基础工具（读写文档）写新 MD 到 `~/.niu/agents/{name}.md`：
   - name 用 kebab-case（如 `photo-organizer`、`doc-summarizer`）
   - frontmatter 填 description / mcpServers / allowAsync 等
   - 正文写系统提示词
3. 当前任务结束。下一轮对话开始时，`chat-with-{name}` 工具自动出现
4. 调用 `chat-with-{name}`（同步或异步）执行任务

### 异步子 Agent

allowAsync: true 的子 Agent 支持异步调用：
- 调用后立即返回"已开始异步工作"，你不阻塞
- 子 Agent 在另一个线程跑，可主动询问你（ask_main_agent）
- 你可随时查询进度（check_subagent_progress）
- 子 Agent 完成后自动汇报，你拿结果判断下一步

### 子 Agent 交互

- 你通过 @子名 给子 Agent 发消息
- 子 Agent 可主动问你（异步模式下）
- 双击停止按钮或 /stop 可终止子 Agent
```

### 组件 4：系统管理手册更新

更新 `AGENTS.md`（项目知识库），加章节"通用子 Agent 体系"：

- 设计目标（减少主 Agent 上下文占用、支持长时任务、支持专业性任务）
- 模板位置和字段说明
- 专用子 Agent（`config/agents/`）vs 通用子 Agent（`~/.niu/agents/`）的区别
- 动态加载机制（`chat()` 入口扫描）
- 主 Agent 创建子 Agent 流程
- 同步 vs 异步调用选择
- 与阶段一/阶段二交互通道的关系
- 维护注意事项（如 MCP 服务器清单更新时同步更新模板）

## 数据流

### 主 Agent 创建子 Agent

```
主 Agent 收到任务"整理我所有照片"
→ 主 Agent LLM 判断：这是复杂长时任务，创建专用子 Agent
→ 主 Agent 读 config/agent-template.md
→ 主 Agent 用基础工具写 ~/.niu/agents/photo-organizer.md
   （frontmatter: mcpServers: [photo-server, lightrag-server], allowAsync: true）
→ 主 Agent 当前轮回复用户"我创建了一个照片整理子 Agent，开始处理"
→ 当前轮结束
```

### 新子 Agent 工具出现

```
用户发新消息（或完成汇报触发新一轮）
→ chat() 入口
→ _refresh_base_tools_schema_if_dirty() 扫 ~/.niu/agents/
→ 发现 photo-organizer.md（不在 _known_user_subagents 集合里）
→ 重算 base_tools_schema → chat-with-photo-organizer 工具出现
→ 主 Agent LLM 看到新工具，调用 chat-with-photo-organizer(async_mode=true)
```

### 异步子 Agent 执行 + 完成汇报

```
chat-with-photo-organizer 异步调用
→ _dispatch_async_subagent → _run_subagent_async（独立线程）
→ 子 Agent 跑（用 photo-server 处理照片）
→ 期间子 Agent 可调 ask_main_agent 询问主 Agent（阶段二能力）
→ 主 Agent 可调 check_subagent_progress 查进度（阶段二能力）
→ 子 Agent 完成 → push "[photo-organizer] 已完成，结果：..." 到 MainAgentRequestQueue
→ db_monitor 链路 A 检测主 Agent 空闲 → 推 SSE
→ 前端调 /api/chat/session → 主 Agent 新一轮 LLM
→ 主 Agent 拿结果判断下一步（继续 / 向用户汇报）
```

### 终止子 Agent

```
用户双击停止按钮 / 主 Agent 发 /stop
→ request_stop_all_subagents（阶段一+二能力）
→ 子 Agent 收到 /stop → LLM 生成总结 → 退出
→ 如果子 Agent 阻塞在 ask_main_agent → cancel_pending_ask 取消 future
→ 5 个死锁约束全部生效（阶段二能力）
```

## 错误处理

1. **MD 文件格式错误**（YAML 解析失败 / frontmatter 缺字段）：`get_subagent_config` 已有 try/except，扩展到 `~/.niu/agents/` 路径，解析失败时 log warning 并跳过该子 Agent（不阻塞其他子 Agent 加载）
2. **`mcpServers` 字段含未加载的服务器**：`get_subagent_mcp_tools_schema` 过滤时找不到对应工具，子 Agent 工具列表会缺这部分——log warning 但不阻塞，主 Agent 调用时子 Agent 会发现自己没工具
3. **`~/.niu/agents/` 目录不存在**：`_refresh_base_tools_schema_if_dirty` 检查 `os.path.isdir`，不存在则跳过
4. **MD 文件名冲突**（`~/.niu/agents/` 与 `config/agents/` 同名）：`_resolve_agent_md_path` 优先返回 `config/agents/` 路径，专用子 Agent 优先
5. **主 Agent 写 MD 失败**（磁盘满 / 权限）：基础工具的文件写入已有错误返回，主 Agent LLM 会看到错误并告知用户

## 测试策略

### 单元测试

- `_resolve_agent_md_path`：项目目录优先 / 用户目录回退 / 都找不到返回 None
- `get_tools_schema` 扫描 `~/.niu/agents/`：空目录 / 多个 MD / 名字去重 / 与 `niu.md` 的 `sub agents` 字段合并
- `_refresh_base_tools_schema_if_dirty`：无变化不重算 / 有新文件重算 / 目录不存在跳过

### 端到端验证（真实 LLM，串联阶段一+二+三能力）

**场景**：主 Agent 创建一个通用子 Agent（异步），让它执行长时任务。

**验证清单**：

**阶段三能力（新增）**：
- [ ] 主 Agent 读 `config/agent-template.md` → 写新 MD 到 `~/.niu/agents/`
- [ ] 下一轮 `chat()` 入口组装上下文时，`chat-with-{newagent}` 工具自动出现
- [ ] 主 Agent 调用新建的子 Agent（同步模式）→ 拿结果返回
- [ ] 主 Agent 调用新建的子 Agent（异步模式）→ 立即返回不阻塞

**阶段一能力（主子 Agent 通信通道）**：
- [ ] 主 Agent 与子 Agent 对话能力（主 Agent 通过 @子名 发消息，子 Agent 收到）
- [ ] 主 Agent 要求子 Agent 终止能力（/stop，子 Agent LLM 生成总结再退出）

**阶段二能力（异步交互 + ask）**：
- [ ] 子 Agent 主动与主 Agent 对话能力（`ask_main_agent`，主 Agent 收到询问并回应）
- [ ] 主 Agent 主动查询子 Agent 工作状态能力（`check_subagent_progress`）
- [ ] 异步子 Agent 完成后自动 push 完成汇报 → 主 Agent 新一轮 LLM 处理（拿结果判断下一步）
- [ ] /stop 在异步子 Agent 阻塞 ask_main_agent 时不死锁（5 个死锁约束全部生效）

**验证方式**：真实 `./niu` 启动 + 真实 LLM 调用 + 真实文件系统操作。禁止 mock。

## 实施顺序

1. 写 `config/agent-template.md`（模板）
2. 改 `agent/subagent.py`：`_resolve_agent_md_path` + `get_subagent_config` / `get_subagent_prompt` 多目录查找
3. 改 `agent/runner.py` `get_tools_schema()`：扫描 `~/.niu/agents/`
4. 改 `agent/runner.py` `NiuRunner.__init__`：初始化 `_known_user_subagents`
5. 改 `agent/runner.py` `chat()`：加 `_refresh_base_tools_schema_if_dirty()`
6. 改 `config/agents/niu.md`：加通用子 Agent 说明段
7. 改 `AGENTS.md`：加通用子 Agent 体系章节
8. 单元测试
9. 端到端真实 LLM 验证

## 不在本阶段范围

- MCP 服务器运行时动态加载（`REQUIRED_SERVERS` 改 yaml 驱动）——独立问题，不影响阶段三
- `tool_lifecycle` 衰减-覆盖评分模式恢复——已挂空，与本阶段无关
- LightRAG 数据韧性方案——投产阻塞问题，独立处理

## 相关文档

- `docs/superpowers/specs/2026-07-02-main-subagent-interaction-design.md` — 阶段一+二设计
- `docs/superpowers/plans/2026-07-03-main-subagent-interaction-stage2.md` — 阶段二实施计划
- `AGENTS.md` — 项目知识库
