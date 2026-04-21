# KG 自动补全系统设计

## 问题陈述

当前知识图谱（KG）存在三类问题：

1. **实体补全缺失**：文档/照片入库时只创建 Document 节点，不提取 Entity。实体提取依赖 dream-evolver 睡眠时处理，每次最多 20 个，不可手动触发
2. **KG 去重缺失**：photo-server 创建 Entity 时不查重，MERGE 只按 `id` 去重不按 `name`，导致重名/重地址/重景点。person ID 格式不统一（`person:{uuid}` vs `person:{name}`）
3. **向量库数据不入 KG**：dream-evolver 工作项 1-5（错误经验、成功经验、工具方言、用户状态、用户画像）只入向量库，KG 中完全缺失，形成信息孤岛

## 设计目标

- 入库后自动补全 KG 实体，无需等待睡眠整理
- 统一 Entity ID 格式，消除重名/重地址问题
- 将向量库中的经验/画像数据同步到 KG，消除信息孤岛
- 子 Agent 纯配置化注册（只改 .md 文件，不改程序）

## 架构概览

```
┌─────────────────────────────────────────────────┐
│              niu_api 进程                         │
│                                                   │
│  ┌──────────────┐  ┌──────────────┐              │
│  │  SkillSync   │  │  KGScanner   │              │
│  │  (已有)      │  │  (新增)      │              │
│  │  60s 扫描    │  │  60s 扫描    │              │
│  │  skills→向量 │  │  KG pending  │              │
│  └──────────────┘  └──────┬───────┘              │
│                            │ 有 pending           │
│                            ▼                      │
│                    ┌──────────────┐               │
│                    │ 内存队列      │               │
│                    │ (FIFO,不持久) │               │
│                    └──────┬───────┘               │
│                            │ 串行消费             │
│                            ▼                      │
│                    ┌──────────────┐               │
│                    │entity-extractor│              │
│                    │  子 Agent     │               │
│                    └──────────────┘               │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│           定时任务系统 (已有)                      │
│                                                   │
│  scheduled_tasks.db:                              │
│  ┌─────────────────────────────────────┐         │
│  │ kg-enricher | 每天8点 | recurring   │         │
│  └──────────────┬──────────────────────┘         │
│                 │ 触发                            │
│                 ▼                                 │
│         ┌──────────────┐                          │
│         │ kg-enricher  │                          │
│         │  子 Agent    │                          │
│         └──────────────┘                          │
└─────────────────────────────────────────────────┘
```

## 一、KGScanner（新增）

**位置**：`agent/injector/kg_scanner.py`

**架构**：与 `SkillSync`（`agent/injector/sync.py`）完全一致 — daemon 线程 + 60s 定时扫描

### 扫描逻辑

每 60 秒扫描 KG 中待处理项：

| 触发条件 | Cypher 查询 | 优先级 |
|----------|------------|--------|
| 新文档无实体 | `entity_status='pending'` | 高 |
| 处理超时 | `entity_status='processing' AND processing_at < now-10min` | 高 |
| 失败重试 | `entity_status='failed' AND retry_count < 3` | 低 |

### 内存队列

- FIFO 队列（`queue.Queue`），不持久化
- 上一个任务完成后再处理下一个（串行）
- 程序关闭后队列丢失，但 pending 节点下次启动自然被发现
- 队列满时跳过新任务（上限 100）

### 处理循环

```python
def _process_loop(self):
    """消费队列，逐个启动 entity-extractor"""
    while not self._stop_event.is_set():
        doc = self._queue.get()  # 阻塞等待
        # 标记 processing
        update_kg(doc.uri, entity_status='processing', processing_at=now)
        # 启动子 Agent（同步等待完成）
        result = call_subagent("entity-extractor", task=...)
        # 根据结果更新状态
        if success:
            update_kg(doc.uri, entity_status='completed')
        else:
            update_kg(doc.uri, entity_status='failed', retry_count+=1)
```

## 二、entity-extractor 子 Agent

**触发方式**：KGScanner 扫描到 pending 后自动启动（程序驱动子 Agent）

**配置文件**：`config/agents/entity-extractor.md`

**MCP 工具**：
- `kg-server/create_entity` — 创建实体
- `kg-server/search_entities` — 查重
- `kg-server/link_document_entity` — 链接文档到实体
- `kg-server/link_entities` — 建立实体间关系
- `kg-server/explore_node` — 探索已有实体关系（避免信息孤岛）

### 场景 A：文档实体提取

1. 读取 Document 的 content（L1 摘要）
2. LLM 提取实体（person, organization, technology, location, concept, other）
3. 对每个实体 `search_entities` 查重（按 name 模糊匹配）
4. 创建新 Entity 或复用已有 Entity（统一 ID 格式）
5. 建立 Document-[MENTIONS]->Entity
6. 同文档实体间建立 RELATED_TO
7. 搜索已有相关实体，建立跨文档关联

### 场景 B：照片 KG 去重与补全

1. 读取 Photo 节点关联的 person Entity
2. 对 `person:{uuid}` 格式的实体：`search_entities(name=人名)` 查重
3. 找到已有 `person:{name}` → 转移 MENTIONS 边，删除 `person:{uuid}` 重复节点
4. 没找到 → 改名为 `person:{name}`（统一 ID 格式）
5. 对 EXIF 中的 location/camera：创建对应 Entity
6. 建立 Photo-[MENTIONS]->Entity 边

## 三、kg-enricher 子 Agent

**触发方式**：定时任务系统驱动（硬写入 `scheduled_tasks.db`）

**初始化**：`niu_api/__main__.py` 启动时检查，不存在则创建：
```python
task_store.create_task(
    id="kg-enricher-daily",
    content="执行知识图谱丰富化：将向量库中的经验、画像、查询模式同步到知识图谱",
    scheduled_at=next_8am,
    is_recurring=True,
    cron_expr="0 8 * * *",
    event_type="recurring",
)
```

**触发后**：调度器调用 `/chat/sync`，主 Agent 识别为 kg-enricher 任务，调用 `chat-with-kg-enricher`

**配置文件**：`config/agents/kg-enricher.md`

**MCP 工具**：
- `kg-server/create_entity` — 创建经验/画像节点
- `kg-server/search_entities` — 查重
- `kg-server/link_entities` — 建立关联
- `kg-server/explore_node` — 探索已有关系
- `vector-store/search_documents` — 搜索向量库数据

### 处理逻辑

1. 查询向量库中 `kg_synced!=true` 的各类别数据
2. 为每条数据创建 KG 节点：
   - `ErrorExperience` — 错误经验（category=document, 含 error_experience 标记）
   - `SuccessExperience` — 成功经验（category=document, 含 success_experience 标记）
   - `UserProfile` — 用户画像（category=interaction_habit, name=user_profile）
   - `InteractionHabit` — 交互习惯（category=interaction_habit, name=user_state）
   - `QueryPattern` — 查询模式（category=query_pattern）
3. 建立关联：
   - Experience-[APPLIES_TO]->Entity（经验涉及的实体）
   - Experience-[RELATED_TO]->Experience（相关经验）
   - Profile-[PREFERS]->Entity（用户偏好的实体/工具）
   - Habit-[TRIGGERS]->QueryPattern（习惯触发的查询模式）
4. 标记 `kg_synced=true`（在向量库 metadata 中）

## 四、防死循环机制

### entity-extractor（KGScanner 驱动）

Document 节点 `entity_status` 状态机：

| 状态 | 含义 | 扫描器行为 |
|------|------|-----------|
| `pending` | 新入库，未处理 | 放入队列 |
| `processing` | 正在处理中 | 跳过 |
| `completed` | 已完成 | 跳过 |
| `failed` | 处理失败 | retry_count < 3 时重试 |
| `failed_permanent` | 永久失败 | 跳过 |

保障：
- 内存队列保证串行处理，不会并发启动多个子 Agent
- processing 超时 10 分钟重置为 pending
- failed 最多重试 3 次

### kg-enricher（定时任务驱动）

- 向量库 `kg_synced=true` 标记已处理
- 每次只处理未标记的数据
- 定时任务系统自身的防重复机制（5分钟触发窗口）

## 五、KG Schema 扩展

### Document 节点新增属性

| 属性 | 类型 | 默认值 | 用途 |
|------|------|--------|------|
| `entity_status` | STRING | `'pending'` | 实体补全状态 |
| `processing_at` | STRING | null | 开始处理时间 |
| `retry_count` | INT64 | 0 | 重试次数 |

### 新增节点类型

| 节点类型 | 属性 | 用途 |
|----------|------|------|
| `ErrorExperience` | id, content, category, created_at | 错误经验 |
| `SuccessExperience` | id, content, category, created_at | 成功经验 |
| `InteractionHabit` | id, content, category, created_at | 交互习惯 |
| `QueryPattern` | id, content, category, created_at | 查询模式 |
| `UserProfile` | id, content, category, created_at | 用户画像 |

### 新增边类型

| 边类型 | 连接 | 属性 | 用途 |
|--------|------|------|------|
| `APPLIES_TO` | Experience -> Entity | confidence | 经验涉及的实体 |
| `PREFERS` | UserProfile -> Entity | confidence | 用户偏好 |
| `TRIGGERS` | InteractionHabit -> QueryPattern | confidence | 习惯触发查询 |

### 统一 Entity ID 规范

| 实体类型 | ID 格式 | 示例 | 变更说明 |
|----------|---------|------|---------|
| person | `person:{name}` | `person:张三` | 不再用 `person:{uuid}` |
| location | `location:{name}` | `location:北京` | 新增 |
| device | `device:{model}` | `device:iPhone 15 Pro` | 新增 |
| technology | `technology:{name}` | `technology:Python` | 统一大小写 |
| organization | `org:{name}` | `org:OpenAI` | 统一前缀 |
| concept | `concept:{name}` | `concept:机器学习` | 统一前缀 |

## 六、子 Agent 纯配置化注册

### 当前问题

添加新子 Agent 需要修改 3 个文件：
1. `config/agents/new-agent.md` — 创建 .md 文件（配置化）
2. `agent/runner.py` 第198行 — 在 `sub_agent_descriptions` 字典中添加条目（硬编码）
3. `agent/handler.py` — 添加 `do_chat_with_new_agent()` 方法（硬编码）

### 现有子 Agent .md 工具审核

改造前需审核现有三个子 Agent 的 .md 文件，确保工具列表完整：

| 子 Agent | mcpServers | .md 列出 | 实际可用 | 缺少 |
|----------|-----------|---------|---------|------|
| file-processor | photo-server | 8 | 9 | unload_face_model（系统级，不需要） |
| event-manager | vector-store, scheduler-server | 6 | 10 | vector-store: get_document, delete_document, list_documents, count_documents（辅助查询，建议补充） |
| context-manager | vector-store, session-manager | 3 | 8 | vector-store: search_documents（已在正文中使用但未在工具章节列出）, get_document, delete_document, list_documents, count_documents（建议补充） |

**改造时需同步更新**：
- `context-manager.md`：在"可用工具"章节补充 search_documents 等向量库查询工具
- `event-manager.md`：补充 vector-store 查询/删除工具说明
- `file-processor.md`：无需修改（unload_face_model 是系统级工具）

### 改造方案

#### 改造点 1：runner.py — 动态读取子 Agent 列表

将 `sub_agent_descriptions` 硬编码字典替换为从 `niu.md` 的 `sub agents` 字段 + 各子 Agent .md 的 `description` 字段动态生成：

```python
def _build_sub_agent_tools():
    """从 niu.md 的 sub agents 字段动态生成子 Agent 工具 schema"""
    niu_config = get_subagent_config("niu")
    sub_agents = niu_config.get("sub agents", [])
    
    tools = []
    for agent_name in sub_agents:
        agent_config = get_subagent_config(agent_name)
        description = agent_config.get("description", f"子 Agent: {agent_name}")
        tools.append({
            "type": "function",
            "function": {
                "name": f"chat-with-{agent_name}",
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string", "description": "任务描述"}
                    },
                    "required": ["task"],
                },
            },
        })
    return tools
```

#### 改造点 2：handler.py — 通用化分发逻辑

在 `NiuHandler.dispatch()` 中增加 `chat-with-*` 通配分支：

```python
# 在 dispatch() 的工具路由部分
if tool_name.startswith("chat-with-"):
    agent_name = tool_name[len("chat-with-"):]
    return (yield from self._call_subagent_gen(agent_name, args))
```

删除 `do_chat_with_file_processor`、`do_chat_with_event_manager`、`do_chat_with_context_manager` 三个硬编码方法。

### 改造后添加新子 Agent 的流程

1. 创建 `config/agents/new-agent.md`（含 front matter 的 `name`、`description`、`mcpServers`、`temperature`）
2. 在 `config/agents/niu.md` 的 `sub agents` 列表中添加 `new-agent`

两步都是改 .md 文件，不需要修改任何 Python 代码。

## 七、dream-evolver 变更

- 删除工作项 7（文档实体补全）— 已由 entity-extractor 替代
- 保留工作项 1-6（错误经验、成功经验、工具方言、用户状态、用户画像、对话 KG 实体/关系）
- 工作项 1-5 的数据后续由 kg-enricher 同步到 KG

## 八、修改文件清单

| 文件 | 修改类型 | 修改内容 |
|------|---------|---------|
| `agent/injector/kg_scanner.py` | 新建 | KGScanner 类（类似 SkillSync） |
| `config/agents/entity-extractor.md` | 新建 | 子 Agent 定义 |
| `config/agents/kg-enricher.md` | 新建 | 子 Agent 定义 |
| `config/agents/dream-evolver.md` | 修改 | 删除工作项 7 |
| `config/agents/niu.md` | 修改 | sub agents 列表新增 entity-extractor、kg-enricher |
| `config/agents/context-manager.md` | 修改 | 补充向量库查询/删除工具说明 |
| `config/agents/event-manager.md` | 修改 | 补充向量库查询/删除工具说明 |
| `agent/runner.py` | 修改 | `sub_agent_descriptions` 改为动态生成 |
| `agent/handler.py` | 修改 | dispatch() 增加 chat-with-* 通配分支，删除硬编码方法 |
| `mcp-servers/kg-server/src/niu_kg_server/__init__.py` | 修改 | 新增节点类型、边类型、entity_status 属性、update_entity_status 工具 |
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | 修改 | sync_to_kg 设置 entity_status='pending'，统一 person ID 格式 |
| `mcp-servers/vector-store/src/niu_vector_store/__init__.py` | 修改 | 新增 update_metadata 工具 |
| `niu_api/__main__.py` | 修改 | 启动 KGScanner，确保 kg-enricher 定时任务存在 |

## 九、实现顺序

1. **子 Agent 纯配置化**（runner.py + handler.py 改造）— 基础设施，先做
2. **审核并更新现有子 Agent .md**（context-manager.md、event-manager.md 补充工具说明）— 配套更新
3. **KG Schema 扩展**（kg-server 新增节点/边/属性）— 数据层
3. **photo-server entity_status**（入库时设置 pending）— 数据生产端
4. **entity-extractor 子 Agent**（.md 文件 + 处理逻辑）— 核心功能
5. **KGScanner**（扫描器 + 内存队列）— 调度层
6. **kg-enricher 子 Agent**（.md 文件 + 处理逻辑）— 丰富化功能
7. **kg-enricher 定时任务注册**（niu_api 启动时初始化）— 调度层
8. **dream-evolver 删除工作项 7** — 清理
9. **vector-store update_metadata 工具** — kg-enricher 依赖
