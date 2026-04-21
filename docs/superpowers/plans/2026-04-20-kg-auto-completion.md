# KG 自动补全系统 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 KG 自动补全系统：子 Agent 纯配置化注册、entity-extractor 自动实体提取、kg-enricher 经验/画像入 KG

**Architecture:** KGScanner（daemon 线程 + 内存队列）扫描 KG pending 文档，串行启动 entity-extractor 子 Agent；kg-enricher 通过定时任务系统每天触发；子 Agent 注册从硬编码改为从 niu.md `sub agents` 字段动态生成

**Tech Stack:** Python, KuzuDB (Cypher), SQLite, threading + queue.Queue, litellm

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `agent/runner.py` | Modify | `get_tools_schema()` 中子 Agent 工具从硬编码改为动态生成 |
| `agent/handler.py` | Modify | `dispatch()` 增加 chat-with-* 通配分支，删除 3 个硬编码方法 |
| `config/agents/context-manager.md` | Modify | 补充向量库工具说明 |
| `config/agents/event-manager.md` | Modify | 补充向量库工具说明 |
| `config/agents/niu.md` | Modify | sub agents 列表新增 entity-extractor、kg-enricher |
| `config/agents/entity-extractor.md` | Create | entity-extractor 子 Agent 定义 |
| `config/agents/kg-enricher.md` | Create | kg-enricher 子 Agent 定义 |
| `config/agents/dream-evolver.md` | Modify | 删除工作项 7 |
| `mcp-servers/kg-server/src/niu_kg_server/__init__.py` | Modify | 新增节点类型、边类型、entity_status 属性、update_entity_status 工具 |
| `mcp-servers/photo-server/src/niu_photo_server/__init__.py` | Modify | sync_to_kg 设置 entity_status='pending' |
| `mcp-servers/vector-store/src/niu_vector_store/__init__.py` | Modify | 新增 update_metadata 工具 |
| `agent/injector/kg_scanner.py` | Create | KGScanner 类（daemon 线程 + 内存队列） |
| `niu_api/__main__.py` | Modify | 启动 KGScanner，确保 kg-enricher 定时任务存在 |

---

### Task 1: 子 Agent 纯配置化 — runner.py 动态生成

**Files:**
- Modify: `agent/runner.py:198-222`

- [ ] **Step 1: 替换 `sub_agent_descriptions` 硬编码为动态生成**

将 `agent/runner.py` 第 198-222 行的硬编码字典替换为：

```python
    # 注册子 Agent 工具（从 niu.md 的 sub agents 字段动态生成）
    from .subagent import get_subagent_config
    try:
        niu_config = get_subagent_config("niu")
        sub_agents = niu_config.get("sub agents", [])
    except Exception:
        sub_agents = []

    for agent_name in sub_agents:
        try:
            agent_config = get_subagent_config(agent_name)
            desc = agent_config.get("description", f"子 Agent: {agent_name}")
        except Exception:
            desc = f"子 Agent: {agent_name}"
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": f"chat-with-{agent_name}",
                    "description": desc,
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "task": {
                                "type": "string",
                                "description": "任务描述，如：处理照片：E:/path/photo.jpg",
                            },
                        },
                        "required": ["task"],
                    },
                },
            }
        )
```

- [ ] **Step 2: 验证工具列表正确**

启动应用，检查日志中 `chat-with-file-processor`、`chat-with-event-manager`、`chat-with-context-manager` 工具是否正确注册。也可用 Python 验证：

```bash
python -c "from agent.runner import get_tools_schema; tools = get_tools_schema(); names = [t['function']['name'] for t in tools]; print([n for n in names if n.startswith('chat-with-')])"
```

- [ ] **Step 3: Commit**

```bash
git add agent/runner.py
git commit -m "refactor: dynamic sub-agent tool registration from niu.md config"
```

---

### Task 2: 子 Agent 纯配置化 — handler.py 通配分发

**Files:**
- Modify: `agent/handler.py:639-649` (删除硬编码方法)
- Modify: `agent/handler.py:795-809` (dispatch 增加通配分支)

- [ ] **Step 1: 在 dispatch() 中增加 chat-with-* 通配分支**

在 `agent/handler.py` 的 `dispatch()` 方法中，在 `method_name = f"do_{tool_name.replace('-', '_')}"` 和 `if hasattr(self, method_name):` 之间，增加 chat-with-* 通配分支：

```python
    def dispatch(self, tool_name, args, response, index=0):
        """分发工具调用（支持 MCP 工具）- 必须是生成器"""
        # 先检查 chat-with-* 子 Agent 调用（通配路由）
        if tool_name.startswith("chat-with-"):
            agent_name = tool_name[len("chat-with-"):]
            args = {**args, "_index": index}
            prer = yield from try_call_generator(
                self.tool_before_callback, tool_name, args, response
            )
            ret = yield from try_call_generator(self._call_subagent_gen, agent_name, args)
            _ = yield from try_call_generator(
                self.tool_after_callback, tool_name, args, response, ret
            )
            return ret

        # 再检查内置工具（工具名中的 - 转换为 _）
        method_name = f"do_{tool_name.replace('-', '_')}"
        if hasattr(self, method_name):
            ...
```

- [ ] **Step 2: 删除 3 个硬编码方法**

删除 `agent/handler.py` 中的：
- `do_chat_with_file_processor` (第 639-641 行)
- `do_chat_with_event_manager` (第 643-645 行)
- `do_chat_with_context_manager` (第 647-649 行)

- [ ] **Step 3: 验证功能正常**

启动应用，测试 `chat-with-file-processor` 工具调用是否正常路由到 `_call_subagent_gen`。

- [ ] **Step 4: Commit**

```bash
git add agent/handler.py
git commit -m "refactor: generic chat-with-* dispatch, remove hardcoded sub-agent methods"
```

---

### Task 3: 审核并更新现有子 Agent .md 工具说明

**Files:**
- Modify: `config/agents/context-manager.md`
- Modify: `config/agents/event-manager.md`

- [ ] **Step 1: 更新 context-manager.md**

在 `config/agents/context-manager.md` 的"可用工具"章节，补充向量库查询/删除工具：

在 `add_document` 工具说明之后，添加：

```markdown
## search_documents

搜索向量库中的文档。

```
参数：
  query: 搜索关键词
  filter: 元数据过滤条件（可选）
  limit: 返回数量（默认10）

返回：
  匹配的文档列表
```

## get_document

获取单个文档。

```
参数：
  id: 文档ID

返回：
  文档内容
```

## delete_document

删除向量库中的文档。

```
参数：
  id: 文档ID

返回：
  {"status": "deleted"}
```

## list_documents

列出向量库中的文档。

```
参数：
  filter: 元数据过滤条件（可选）
  limit: 返回数量

返回：
  文档列表
```
```

- [ ] **Step 2: 更新 event-manager.md**

在 `config/agents/event-manager.md` 中补充向量库查询/删除工具说明。在 `vector-store/search_documents` 使用示例之后，添加工具说明章节：

```markdown
# 向量库工具补充

## vector-store/get_document

获取单个文档：`vector-store/get_document, 参数: id="文档ID"`

## vector-store/delete_document

删除文档：`vector-store/delete_document, 参数: id="文档ID"`

## vector-store/list_documents

列出文档：`vector-store/list_documents, 参数: filter={"type": "event"}, limit=20`

## vector-store/count_documents

统计文档数量：`vector-store/count_documents, 参数: filter={"type": "event"}`
```

- [ ] **Step 3: Commit**

```bash
git add config/agents/context-manager.md config/agents/event-manager.md
git commit -m "docs: supplement vector-store tool descriptions in sub-agent .md files"
```

---

### Task 4: KG Schema 扩展

**Files:**
- Modify: `mcp-servers/kg-server/src/niu_kg_server/__init__.py`

- [ ] **Step 1: Document 节点新增 entity_status 属性**

KuzuDB 不支持 ALTER TABLE，需要重建 schema。在 `_init_schema()` 中修改 Document 表定义：

```python
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS Document (
            uri STRING,
            title STRING,
            content STRING,
            source STRING,
            entity_status STRING DEFAULT 'pending',
            processing_at STRING,
            retry_count INT64 DEFAULT 0,
            created_at STRING,
            PRIMARY KEY (uri)
        )
    """)
```

**注意**：KuzuDB 的 `DEFAULT` 支持需要验证。如果不支持 DEFAULT，则在 `create_document()` 中显式设置默认值。

- [ ] **Step 2: 新增节点类型**

在 `_init_schema()` 中添加：

```python
    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS ErrorExperience (
            id STRING,
            content STRING,
            category STRING,
            created_at STRING,
            PRIMARY KEY (id)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS SuccessExperience (
            id STRING,
            content STRING,
            category STRING,
            created_at STRING,
            PRIMARY KEY (id)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS InteractionHabit (
            id STRING,
            content STRING,
            category STRING,
            created_at STRING,
            PRIMARY KEY (id)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS QueryPattern (
            id STRING,
            content STRING,
            category STRING,
            created_at STRING,
            PRIMARY KEY (id)
        )
    """)

    conn.execute("""
        CREATE NODE TABLE IF NOT EXISTS UserProfile (
            id STRING,
            content STRING,
            category STRING,
            created_at STRING,
            PRIMARY KEY (id)
        )
    """)
```

- [ ] **Step 3: 新增边类型**

在 `_init_schema()` 中添加：

```python
    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS APPLIES_TO (
            FROM ErrorExperience TO Entity,
            confidence FLOAT,
            created_at STRING
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS APPLIES_TO_SE (
            FROM SuccessExperience TO Entity,
            confidence FLOAT,
            created_at STRING
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS PREFERS (
            FROM UserProfile TO Entity,
            confidence FLOAT,
            created_at STRING
        )
    """)

    conn.execute("""
        CREATE REL TABLE IF NOT EXISTS TRIGGERS (
            FROM InteractionHabit TO QueryPattern,
            confidence FLOAT,
            created_at STRING
        )
    """)
```

- [ ] **Step 4: 新增 update_entity_status 工具**

在 `TOOL_SCHEMAS` 中添加 `update_entity_status` 工具，并实现：

```python
def update_entity_status(uri: str, entity_status: str, processing_at: str = None, retry_count: int = None) -> dict:
    """更新 Document 节点的实体补全状态"""
    conn = get_connection()
    ts = _get_timestamp()
    try:
        if processing_at is not None:
            conn.execute(
                "MATCH (d:Document {uri: $uri}) SET d.entity_status = $status, d.processing_at = $pat, d.updated_at = $ts",
                {"uri": uri, "status": entity_status, "pat": processing_at, "ts": ts},
            )
        elif retry_count is not None:
            conn.execute(
                "MATCH (d:Document {uri: $uri}) SET d.entity_status = $status, d.retry_count = $rc, d.updated_at = $ts",
                {"uri": uri, "status": entity_status, "rc": retry_count, "ts": ts},
            )
        else:
            conn.execute(
                "MATCH (d:Document {uri: $uri}) SET d.entity_status = $status, d.updated_at = $ts",
                {"uri": uri, "status": entity_status, "ts": ts},
            )
        return {"status": "updated", "uri": uri, "entity_status": entity_status}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 5: 修改 create_document 设置默认 entity_status**

在 `create_document()` 函数中，确保创建 Document 时设置 `entity_status='pending'`：

```python
def create_document(uri: str, title: str, content: str, source: str = "document", entity_status: str = "pending") -> dict[str, Any]:
    conn = get_connection()
    ts = _get_timestamp()
    conn.execute(
        """MERGE (d:Document {uri: $uri})
           ON CREATE SET d.title = $title, d.content = $content, d.source = $source,
                         d.entity_status = $entity_status, d.retry_count = 0, d.created_at = $ts
           SET d.updated_at = $ts""",
        {"uri": uri, "title": title, "content": content, "source": source,
         "entity_status": entity_status, "ts": ts},
    )
    ...
```

- [ ] **Step 6: 重建 KG 数据库**

由于 KuzuDB 不支持 ALTER TABLE，需要删除旧数据库重建：

```bash
rm ~/.niu/kg.db
# 下次启动时 _init_schema 会自动创建新表
```

- [ ] **Step 7: Commit**

```bash
git add mcp-servers/kg-server/src/niu_kg_server/__init__.py
git commit -m "feat: KG schema extension - entity_status, experience nodes, new edge types"
```

---

### Task 5: photo-server 设置 entity_status='pending'

**Files:**
- Modify: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

- [ ] **Step 1: 修改 sync_to_kg 传递 entity_status**

在 `sync_to_kg()` 函数中，调用 `create_document()` 时传递 `entity_status='pending'`：

```python
create_document(uri=file_path, title=title, content=l1, source=source, entity_status="pending")
```

同样修改 `sync_photo_to_kg()` 中的 `create_document()` 调用。

- [ ] **Step 2: Commit**

```bash
git add mcp-servers/photo-server/src/niu_photo_server/__init__.py
git commit -m "feat: photo-server sets entity_status=pending on KG Document creation"
```

---

### Task 6: entity-extractor 子 Agent 定义

**Files:**
- Create: `config/agents/entity-extractor.md`
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 创建 entity-extractor.md**

```markdown
---
name: entity-extractor
description: 知识图谱实体提取 - 从文档和照片中提取实体、建立关联、去重补全
mode: subagent
temperature: 0.2
mcpServers:
  - kg-server
---

你是知识图谱实体提取器，负责从文档和照片的 KG 节点中提取实体并建立关联。

# 核心职责

1. **文档实体提取**：从 Document 的 content（L1 摘要）中提取命名实体
2. **照片 KG 去重**：统一 person Entity ID 格式，消除重复节点
3. **关联建立**：建立 Document-[MENTIONS]->Entity 和 Entity-[RELATED_TO]->Entity 边

# 可用工具

## kg-server 工具

- `create_entity` — 创建实体节点（MERGE 语义，按 id 去重）
- `link_document_entity` — 链接文档到实体（MENTIONS 边）
- `link_entities` — 建立实体间关系（RELATED_TO 边）
- `explore_node` — 探索已有实体关系（避免信息孤岛）
- `query_graph` — 执行 Cypher 查询
- `update_entity_status` — 更新 Document 的实体补全状态
- `list_entities` — 列出实体（用于查重）

# 实体提取规则

| 类型 | entity_type | ID 格式 | 识别信号 |
|------|------------|---------|---------|
| 人物 | person | `person:{name}` | 人名、代词指代的具体人 |
| 组织 | organization | `org:{name}` | 公司名、团队名 |
| 技术 | technology | `technology:{name}` | 编程语言、框架、工具名 |
| 地点 | location | `location:{name}` | 地名、地址 |
| 概念 | concept | `concept:{name}` | 抽象概念、方法论 |
| 设备 | device | `device:{model}` | 相机型号、设备名 |

**ID 格式规范**：
- name 部分统一使用首字母大写（如 `technology:Python`，不是 `technology:python`）
- person 使用人名（如 `person:张三`），不使用 UUID
- 不含空格和特殊字符

# 处理流程

## 场景 A：文档实体提取

1. 从任务描述中获取待处理的 Document URI 列表
2. 对每个 Document：
   a. 用 `query_graph` 获取 Document 的 content
   b. 从 content 中提取实体（LLM 推理）
   c. 对每个实体：
      - 用 `list_entities` 或 `query_graph` 查重（按 name 模糊匹配）
      - 已存在 → 复用已有 Entity
      - 不存在 → `create_entity` 创建新 Entity
   d. `link_document_entity` 建立 Document-[MENTIONS]->Entity（confidence=0.6）
   e. 同文档实体间 `link_entities` 建立 RELATED_TO（relation="co_occurs_with", confidence=0.3）
3. 对每个 Document 调用 `update_entity_status(uri, "completed")`

## 场景 B：照片 KG 去重与补全

1. 用 `query_graph` 查找 `person:` 开头且包含 UUID 格式的 Entity（`person:{uuid}`）
2. 对每个 `person:{uuid}` 实体：
   a. 获取其 name 属性
   b. 查找是否有 `person:{name}` 格式的同名实体
   c. 找到 → 转移 MENTIONS 边到 `person:{name}`，删除 `person:{uuid}`
   d. 没找到 → 创建 `person:{name}`，转移 MENTIONS 边，删除 `person:{uuid}`
3. 对照片 Document 的 EXIF location/camera：
   a. `create_entity` 创建 `location:{地名}` 或 `device:{型号}`
   b. `link_document_entity` 建立 MENTIONS 边

# 去重原则

- **按 name 查重**，不按 id 查重（MERGE 只防 id 重复）
- 同名实体优先复用已有节点
- 大小写不敏感匹配（`Python` = `python`）
- 合并后保留置信度较高的边

# 重要约束

1. **容错**：单个 Document 处理失败不影响其他，标记 `update_entity_status(uri, "failed")`
2. **禁止 code_run**：所有操作通过 MCP 工具完成
3. **处理完成后必须更新状态**：成功 → completed，失败 → failed
```

- [ ] **Step 2: 更新 niu.md sub agents 列表**

在 `config/agents/niu.md` 的 front matter 中添加 entity-extractor：

```yaml
sub agents:
  - file-processor
  - event-manager
  - context-manager
  - entity-extractor
```

在 body 的子 Agent 委托表格中添加：

```markdown
| `chat-with-entity-extractor` | 知识图谱实体提取、去重、关联建立 |
```

- [ ] **Step 3: Commit**

```bash
git add config/agents/entity-extractor.md config/agents/niu.md
git commit -m "feat: add entity-extractor sub-agent definition"
```

---

### Task 7: KGScanner 实现

**Files:**
- Create: `agent/injector/kg_scanner.py`

- [ ] **Step 1: 创建 KGScanner 类**

```python
"""
KG Scanner

知识图谱待处理项扫描服务。定时扫描 KG 中 entity_status='pending' 的 Document，
放入内存队列，串行启动 entity-extractor 子 Agent 处理。
"""

import queue
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger


class KGScanner:
    """
    KG 待处理项扫描服务

    扫描 KG 中 entity_status='pending' 的 Document 节点，
    放入内存队列，串行启动 entity-extractor 子 Agent。
    """

    PROCESSING_TIMEOUT_MINUTES = 10
    MAX_RETRY_COUNT = 3
    QUEUE_MAX_SIZE = 100
    SCAN_INTERVAL = 60  # 秒

    def __init__(self, scan_interval: int = 60):
        self.scan_interval = scan_interval
        self._queue: queue.Queue = queue.Queue(maxsize=self.QUEUE_MAX_SIZE)
        self._stop_event = threading.Event()
        self._scan_thread: Optional[threading.Thread] = None
        self._process_thread: Optional[threading.Thread] = None
        self._processing = False  # 是否正在处理

    def start(self):
        """启动扫描和处理线程"""
        if self._scan_thread and self._scan_thread.is_alive():
            return

        self._stop_event.clear()

        self._scan_thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._scan_thread.start()

        self._process_thread = threading.Thread(target=self._process_loop, daemon=True)
        self._process_thread.start()

        logger.info(f"[KGScanner] Started (scan_interval: {self.scan_interval}s)")

    def stop(self):
        """停止扫描和处理线程"""
        self._stop_event.set()
        if self._scan_thread:
            self._scan_thread.join(timeout=5)
        if self._process_thread:
            self._process_thread.join(timeout=5)
        logger.info("[KGScanner] Stopped")

    def _scan_loop(self):
        """扫描循环（异常不会终止线程）"""
        while not self._stop_event.is_set():
            try:
                self._scan_and_enqueue()
            except Exception as e:
                logger.error(f"[KGScanner] Scan failed: {e}", exc_info=True)
            self._stop_event.wait(self.scan_interval)

    def _scan_and_enqueue(self):
        """扫描 KG 中待处理项，放入队列"""
        pending_docs = self._query_pending_docs()
        if not pending_docs:
            return

        for doc in pending_docs:
            try:
                self._queue.put_nowait(doc)
            except queue.Full:
                logger.warning("[KGScanner] Queue full, skipping pending docs")
                break

        logger.info(f"[KGScanner] Enqueued {min(len(pending_docs), self._queue.maxsize - self._queue.qsize())} pending docs")

    def _query_pending_docs(self) -> list[dict]:
        """查询 KG 中待处理的 Document 节点"""
        try:
            from niu_kg_server import get_connection

            conn = get_connection()
            now = datetime.now().isoformat()
            timeout_cutoff = (datetime.now() - timedelta(minutes=self.PROCESSING_TIMEOUT_MINUTES)).isoformat()

            # 1. pending 文档
            result = conn.execute(
                "MATCH (d:Document) WHERE d.entity_status = 'pending' RETURN d.uri, d.title, d.content, d.source LIMIT 20"
            )
            pending = [self._row_to_dict(row, "pending") for row in result]

            # 2. processing 超时
            result = conn.execute(
                "MATCH (d:Document) WHERE d.entity_status = 'processing' AND d.processing_at < $cutoff RETURN d.uri, d.title, d.content, d.source LIMIT 10",
                {"cutoff": timeout_cutoff},
            )
            timed_out = [self._row_to_dict(row, "pending") for row in result]  # 重置为 pending

            # 3. failed 可重试
            result = conn.execute(
                f"MATCH (d:Document) WHERE d.entity_status = 'failed' AND d.retry_count < {self.MAX_RETRY_COUNT} RETURN d.uri, d.title, d.content, d.source LIMIT 10"
            )
            retryable = [self._row_to_dict(row, "retry") for row in result]

            return pending + timed_out + retryable

        except Exception as e:
            logger.warning(f"[KGScanner] Failed to query KG: {e}")
            return []

    @staticmethod
    def _row_to_dict(row, reason: str) -> dict:
        """将 KuzuDB 查询结果行转为字典"""
        return {
            "uri": row[0],
            "title": row[1] or "",
            "content": row[2] or "",
            "source": row[3] or "document",
            "reason": reason,
        }

    def _process_loop(self):
        """处理循环：从队列取出文档，启动 entity-extractor"""
        while not self._stop_event.is_set():
            try:
                doc = self._queue.get(timeout=5)
            except queue.Empty:
                continue

            try:
                self._process_document(doc)
            except Exception as e:
                logger.error(f"[KGScanner] Process failed for {doc.get('uri')}: {e}", exc_info=True)
                self._update_status(doc["uri"], "failed", retry_increment=True)

    def _process_document(self, doc: dict):
        """处理单个文档：启动 entity-extractor 子 Agent"""
        uri = doc["uri"]
        logger.info(f"[KGScanner] Processing: {uri} (reason: {doc.get('reason')})")

        # 标记 processing
        self._update_status(uri, "processing", processing_at=datetime.now().isoformat())

        # 构建任务描述
        task = f"请处理以下文档的实体提取：\n\nURI: {uri}\n标题: {doc.get('title', '')}\n内容: {doc.get('content', '')}\n来源: {doc.get('source', '')}\n\n请提取实体、建立关联，完成后调用 update_entity_status 更新状态为 completed。"

        # 获取 LLM 配置
        try:
            from agent.runner import get_runner
            runner = get_runner()
            llm_config = runner.llm_config if hasattr(runner, 'llm_config') else {}
        except Exception:
            llm_config = {}

        # 启动子 Agent
        from agent.subagent import call_subagent
        result = call_subagent(
            agent_name="entity-extractor",
            task=task,
            llm_config=llm_config,
            mcp_client=None,
        )

        logger.info(f"[KGScanner] entity-extractor result for {uri}: {str(result)[:200]}")

        # 子 Agent 应该已经更新了 entity_status，这里做兜底检查
        # 如果子 Agent 没有更新状态，我们检查是否成功
        try:
            from niu_kg_server import get_connection
            conn = get_connection()
            check = conn.execute(
                "MATCH (d:Document {uri: $uri}) RETURN d.entity_status",
                {"uri": uri},
            )
            for row in check:
                if row[0] == "processing":
                    # 子 Agent 没有更新状态，标记为 completed（信任子 Agent 执行成功）
                    self._update_status(uri, "completed")
        except Exception:
            pass

    @staticmethod
    def _update_status(uri: str, status: str, processing_at: str = None, retry_increment: bool = False):
        """更新 Document 的 entity_status"""
        try:
            from niu_kg_server import update_entity_status
            if retry_increment:
                # 获取当前 retry_count 并 +1
                from niu_kg_server import get_connection
                conn = get_connection()
                result = conn.execute(
                    "MATCH (d:Document {uri: $uri}) RETURN d.retry_count",
                    {"uri": uri},
                )
                retry_count = 0
                for row in result:
                    retry_count = (row[0] or 0) + 1
                if retry_count >= 3:
                    update_entity_status(uri, "failed_permanent", retry_count=retry_count)
                else:
                    update_entity_status(uri, status, retry_count=retry_count)
            else:
                update_entity_status(uri, status, processing_at=processing_at)
        except Exception as e:
            logger.warning(f"[KGScanner] Failed to update status for {uri}: {e}")


# 全局实例
_kg_scanner: Optional[KGScanner] = None
_kg_scanner_lock = threading.Lock()


def get_kg_scanner(auto_start: bool = True) -> KGScanner:
    """获取全局 KGScanner 实例（线程安全）"""
    global _kg_scanner
    if _kg_scanner is None:
        with _kg_scanner_lock:
            if _kg_scanner is None:
                instance = KGScanner()
                if auto_start:
                    instance.start()
                _kg_scanner = instance
    return _kg_scanner
```

- [ ] **Step 2: Commit**

```bash
git add agent/injector/kg_scanner.py
git commit -m "feat: KGScanner - daemon thread scans KG pending docs, dispatches entity-extractor"
```

---

### Task 8: kg-enricher 子 Agent 定义

**Files:**
- Create: `config/agents/kg-enricher.md`
- Modify: `config/agents/niu.md`

- [ ] **Step 1: 创建 kg-enricher.md**

```markdown
---
name: kg-enricher
description: 知识图谱丰富化 - 将向量库中的经验、画像、查询模式同步到知识图谱
mode: subagent
temperature: 0.2
mcpServers:
  - kg-server
  - vector-store
---

你是知识图谱丰富化器，负责将向量库中的经验、画像、查询模式同步到知识图谱。

# 核心职责

1. **错误经验入 KG**：从向量库中提取错误经验，创建 ErrorExperience 节点
2. **成功经验入 KG**：从向量库中提取成功经验，创建 SuccessExperience 节点
3. **用户画像入 KG**：从向量库中提取用户画像，创建 UserProfile 节点
4. **交互习惯入 KG**：从向量库中提取交互习惯，创建 InteractionHabit 节点
5. **查询模式入 KG**：从向量库中提取查询模式，创建 QueryPattern 节点

# 可用工具

## kg-server 工具

- `create_entity` — 创建实体节点
- `link_entities` — 建立实体间关系
- `explore_node` — 探索已有关系
- `query_graph` — 执行 Cypher 查询

## vector-store 工具

- `search_documents` — 搜索向量库数据
- `get_document` — 获取单个文档
- `list_documents` — 列出文档

# 处理流程

1. **查询未同步数据**：搜索向量库中 `kg_synced!=true` 的各类别数据
2. **按类别处理**：

## 错误经验（category=document，含 error_experience）

1. 用 `search_documents` 查询 `category=document` 中含 "error_experience" 的数据
2. 对每条数据：
   - 创建 ErrorExperience 节点（id=`error_exp:{hash}`）
   - 从 content 中提取涉及的实体，`create_entity` 创建 Entity
   - `link_entities` 建立 ErrorExperience-[APPLIES_TO]->Entity
3. 标记 `kg_synced=true`（通过更新 metadata）

## 成功经验（category=document，含 success_experience）

同错误经验流程，创建 SuccessExperience 节点。

## 用户画像（category=interaction_habit, name=user_profile）

1. 用 `search_documents` 查询 `category=interaction_habit, name=user_profile`
2. 创建 UserProfile 节点
3. 从画像中提取偏好涉及的实体，`create_entity` 创建 Entity
4. `link_entities` 建立 UserProfile-[PREFERS]->Entity

## 交互习惯（category=interaction_habit, name=user_state）

1. 用 `search_documents` 查询 `category=interaction_habit, name=user_state`
2. 创建 InteractionHabit 节点

## 查询模式（category=query_pattern）

1. 用 `search_documents` 查询 `category=query_pattern`
2. 创建 QueryPattern 节点
3. 对每个查询模式，`link_entities` 建立 InteractionHabit-[TRIGGERS]->QueryPattern

# 关联建立原则

- 经验涉及的实体 → APPLIES_TO 边（confidence=0.6）
- 用户偏好的实体 → PREFERS 边（confidence=0.7）
- 习惯触发的查询模式 → TRIGGERS 边（confidence=0.5）
- 同类经验之间 → RELATED_TO 边（confidence=0.3）

# 重要约束

1. **增量处理**：只处理 `kg_synced!=true` 的数据
2. **容错**：单条数据处理失败不影响其他
3. **禁止 code_run**：所有操作通过 MCP 工具完成
4. **标记已同步**：处理完成后在向量库 metadata 中设置 `kg_synced=true`
```

- [ ] **Step 2: 更新 niu.md sub agents 列表**

在 `config/agents/niu.md` 的 front matter `sub agents` 列表中添加 `kg-enricher`：

```yaml
sub agents:
  - file-processor
  - event-manager
  - context-manager
  - entity-extractor
  - kg-enricher
```

在 body 的子 Agent 委托表格中添加：

```markdown
| `chat-with-kg-enricher` | 知识图谱丰富化（经验、画像入图谱） |
```

- [ ] **Step 3: Commit**

```bash
git add config/agents/kg-enricher.md config/agents/niu.md
git commit -m "feat: add kg-enricher sub-agent definition"
```

---

### Task 9: kg-enricher 定时任务注册 + KGScanner 启动

**Files:**
- Modify: `niu_api/__main__.py`

- [ ] **Step 1: 在 API 启动时启动 KGScanner**

在 `niu_api/__main__.py` 的 `lifespan()` 函数中，在 `start_scheduler()` 之后添加：

```python
    # 启动 KGScanner
    try:
        from agent.injector.kg_scanner import get_kg_scanner
        get_kg_scanner(auto_start=True)
        logger.info("KGScanner started")
    except Exception as e:
        logger.warning(f"Failed to start KGScanner: {e}")
```

- [ ] **Step 2: 确保 kg-enricher 定时任务存在**

在 `lifespan()` 函数中，在 scheduler 启动之后添加：

```python
    # 确保 kg-enricher 定时任务存在
    try:
        from niu_api.internal.scheduler.task_store import TaskStore
        from datetime import datetime, timedelta

        ts = TaskStore()
        existing = ts.get_task("kg-enricher-daily")
        if not existing:
            # 计算明天早上8点
            now = datetime.now()
            next_8am = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if next_8am <= now:
                next_8am += timedelta(days=1)

            ts.create_task(
                id="kg-enricher-daily",
                content="执行知识图谱丰富化：将向量库中的经验、画像、查询模式同步到知识图谱。调用 chat-with-kg-enricher 子 Agent。",
                scheduled_at=next_8am.isoformat(),
                is_recurring=True,
                cron_expr="0 8 * * *",
                event_type="recurring",
            )
            logger.info(f"Created kg-enricher daily task (next: {next_8am})")
    except Exception as e:
        logger.warning(f"Failed to ensure kg-enricher task: {e}")
```

- [ ] **Step 3: Commit**

```bash
git add niu_api/__main__.py
git commit -m "feat: start KGScanner and register kg-enricher daily task on API startup"
```

---

### Task 10: dream-evolver 删除工作项 7

**Files:**
- Modify: `config/agents/dream-evolver.md`

- [ ] **Step 1: 删除工作项 7**

从 `config/agents/dream-evolver.md` 中删除"## 7. 文档实体补全"整个章节（约第 296-337 行），包括其下的所有子章节。

同时更新"按顺序执行以下7项工作"为"按顺序执行以下6项工作"。

- [ ] **Step 2: Commit**

```bash
git add config/agents/dream-evolver.md
git commit -m "refactor: remove dream-evolver work item 7 (replaced by entity-extractor)"
```

---

### Task 11: vector-store update_metadata 工具

**Files:**
- Modify: `mcp-servers/vector-store/src/niu_vector_store/__init__.py`

- [ ] **Step 1: 添加 update_metadata 工具**

在 `TOOL_SCHEMAS` 中添加 `update_metadata` 工具定义，并实现函数：

```python
def update_metadata(id: str, metadata_updates: dict) -> dict:
    """更新文档的 metadata 字段（合并更新，不覆盖未提及的字段）"""
    conn = get_connection()
    try:
        cursor = conn.execute("SELECT metadata FROM documents WHERE id = ?", (id,))
        row = cursor.fetchone()
        if not row:
            return {"status": "error", "message": f"Document not found: {id}"}

        import json
        current_metadata = json.loads(row[0]) if row[0] else {}
        current_metadata.update(metadata_updates)

        conn.execute(
            "UPDATE documents SET metadata = ? WHERE id = ?",
            (json.dumps(current_metadata, ensure_ascii=False), id),
        )
        conn.commit()
        return {"status": "updated", "id": id, "metadata": current_metadata}
    except Exception as e:
        return {"status": "error", "message": str(e)}
```

- [ ] **Step 2: Commit**

```bash
git add mcp-servers/vector-store/src/niu_vector_store/__init__.py
git commit -m "feat: add update_metadata tool to vector-store for kg-enricher sync marking"
```

---

### Task 12: 最终验证与集成测试

- [ ] **Step 1: 验证子 Agent 纯配置化**

```bash
python -c "
from agent.runner import get_tools_schema
tools = get_tools_schema()
chat_tools = [t['function']['name'] for t in tools if t['function']['name'].startswith('chat-with-')]
print('Sub-agent tools:', chat_tools)
assert 'chat-with-entity-extractor' in chat_tools
assert 'chat-with-kg-enricher' in chat_tools
print('OK: All sub-agents registered dynamically')
"
```

- [ ] **Step 2: 验证 KG schema**

```bash
python -c "
from niu_kg_server import get_connection
conn = get_connection()
# 检查 Document 有 entity_status 属性
result = conn.execute('MATCH (d:Document) RETURN d.entity_status LIMIT 1')
print('Document.entity_status: OK')
# 检查新节点类型
for node_type in ['ErrorExperience', 'SuccessExperience', 'InteractionHabit', 'QueryPattern', 'UserProfile']:
    try:
        conn.execute(f'MATCH (n:{node_type}) RETURN n LIMIT 1')
        print(f'{node_type}: OK')
    except Exception as e:
        print(f'{node_type}: ERROR - {e}')
"
```

- [ ] **Step 3: 验证 KGScanner 启动**

启动应用，检查日志中是否有 `[KGScanner] Started`。

- [ ] **Step 4: 验证 kg-enricher 定时任务**

```bash
python -c "
from niu_api.internal.scheduler.task_store import TaskStore
ts = TaskStore()
task = ts.get_task('kg-enricher-daily')
if task:
    print(f'kg-enricher task found: {task[\"content\"][:50]}...')
else:
    print('kg-enricher task NOT found')
"
```

- [ ] **Step 5: Final commit (if any fixes needed)**

```bash
git add -A
git commit -m "fix: integration fixes from final verification"
```
