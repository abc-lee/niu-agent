# 便签系统重构：SQLite → JSON + LightRAG

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 去掉便签的 SQLite 存储，改为 JSON 文件存储，通过 SkillSync 扫描自动同步到 LightRAG，Agent 通过 bash + Skill 操作便签。

**Architecture:** 便签以 JSON 数组存储在 workspace 目录，前端 API 改为读写 JSON 文件。SkillSync 扫描时增加便签文件处理，便签作为 knowledge 类型实体入库 LightRAG。删除所有 SQLite 相关代码。

**Tech Stack:** Python (FastAPI, LightRAG), JSON, SkillSync

---

## 1. 现状分析

### 当前架构

```
前端 (sticky.html)
    ↓ REST API
niu_api/notes_api.py
    ↓
niu_api/notes.py → ~/.niu/notes.db (SQLite)
    ↓ BackgroundTask
sync_note_to_kg() → LightRAG ainsert
```

### 当前数据模型

```sql
CREATE TABLE notes (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT
)
```

极简 schema：无 title、tags、category 字段。

### 涉及文件清单

| 文件 | 作用 | 操作 |
|------|------|------|
| `niu_api/notes.py` | SQLite 数据层 | **删除** |
| `niu_api/notes_api.py` | REST API 端点 + LightRAG 同步 | **重写** |
| `niu_api/__main__.py` | 启动时初始化 notes DB | **移除初始化代码** |
| `ui/assistant/sticky.html` | 便签 UI | **保留，改 API 调用** |
| `ui/assistant/main.js` | Electron IPC 便签处理 | **保留** |
| `ui/assistant/preload-sticky.js` | 便签窗口 preload | **保留** |
| `agent/injector/sync.py` | SkillSync 扫描 | **增加便签扫描** |
| `niu_api/internal/lightrag_pipeline.py` | `[Note:]` 前缀处理 | **移除 note 分支** |
| `tests/test_phase02_lightrag_migration.py` | 便签迁移测试 | **更新** |
| `tests/test_lightrag_pipeline.py` | `[Note:]` 前缀测试 | **更新** |

---

## 2. 目标架构

```
前端 (sticky.html)
    ↓ REST API
niu_api/notes_api.py
    ↓ 读写 JSON 文件
{workspace}/notes/notes.json
    ↓ SkillSync 扫描
LightRAG ainsert (knowledge 类型)
    ↓ 检索时
Agent 通过 LightRAG search 获取便签内容
Agent 通过 bash + Skill 直接读写 notes.json
```

### 数据模型

```json
[
  {
    "id": "abc123",
    "content": "记得周五前提交报告",
    "tags": ["工作", "提醒"],
    "created_at": "2026-04-26T10:30:00",
    "updated_at": "2026-04-26T14:20:00"
  }
]
```

新增 `tags` 字段（字符串数组），其余字段保持兼容。

---

## 3. 组件设计

### 3.1 JSON 存储层

**文件**：`niu_api/notes.py`（重写）

职责：读写 `{workspace}/notes/notes.json`

```python
def _get_notes_path() -> Path:
    """返回 {workspace}/notes/notes.json 路径"""

def read_notes() -> list[dict]:
    """读取所有便签，文件不存在返回空列表"""

def write_notes(notes: list[dict]) -> None:
    """原子写入所有便签（先写临时文件再 rename）"""

def create_note(note_id: str, content: str, tags: list[str] = None) -> dict:
    """追加一条便签"""

def update_note(note_id: str, content: str = None, tags: list[str] = None) -> dict:
    """更新便签内容或标签"""

def delete_note(note_id: str) -> dict:
    """删除便签，同时删除 LightRAG 实体"""

def list_notes() -> list[dict]:
    """列出所有便签"""

def get_note(note_id: str) -> dict | None:
    """获取单条便签"""
```

关键设计决策：
- workspace 路径从环境变量 `WORKSPACE_PATH` 获取（与 MCP 服务器一致）
- 文件不存在时返回空列表，不抛异常
- 写入使用临时文件 + rename 保证原子性
- `delete_note()` 同步调用 `LightRAGAdapter.delete_entity(f"note:{note_id}")`

### 3.2 API 端点

**文件**：`niu_api/notes_api.py`（重写）

端点保持不变（前端不改动）：

| 方法 | 路径 | 变化 |
|------|------|------|
| POST | `/api/notes` | 改为调用 JSON 存储 + ainsert |
| GET | `/api/notes` | 改为调用 JSON 存储 |
| GET | `/api/notes/{note_id}` | 改为调用 JSON 存储 |
| PUT | `/api/notes/{note_id}` | 改为调用 JSON 存储 + ainsert |
| DELETE | `/api/notes/{note_id}` | 改为调用 JSON 存储（内部已含 delete_entity） |

请求模型增加 `tags` 字段（可选）：

```python
class NoteCreateRequest(BaseModel):
    id: str
    content: str
    tags: list[str] = []
    createdAt: float

class NoteUpdateRequest(BaseModel):
    id: str
    content: str
    tags: list[str] = []
    updatedAt: float
```

LightRAG 同步方式：
- 创建/更新时调用 `LightRAGAdapter` 的 `ainsert()` 插入便签全文
- 删除时调用 `LightRAGAdapter.delete_entity(f"note:{note_id}")`
- 不再使用 `[Note: {id}]` 前缀，改为使用 LightRAG 的结构化实体插入

### 3.3 SkillSync 便签扫描

**文件**：`agent/injector/sync.py`

在 SkillSync 的 `_scan_all()` 方法中增加对 `notes/notes.json` 的处理：

```python
def _scan_notes(self):
    """扫描 workspace/notes/notes.json，将变化同步到 LightRAG"""
    notes_path = self._workspace / "notes" / "notes.json"
    if not notes_path.exists():
        return
    notes = json.loads(notes_path.read_text(encoding="utf-8"))
    for note in notes:
        note_id = note["id"]
        if note_id in self._last_notes_scan:
            continue  # 无变化
        # ainsert 到 LightRAG
        self._inject_note_to_lightrag(note_id, note["content"], note.get("tags", []))
        self._last_notes_scan.add(note_id)
```

便签作为 `knowledge` 类型实体入库，命名格式 `note:{id}`。

### 3.4 Agent Skill 文件

**文件**：`memory/skills/note-management.md`

指导 Agent 如何通过 bash 操作便签：

```markdown
---
name: note-management
description: Use when user asks to create, read, update, delete, or search sticky notes/便签
---

# 便签管理

便签存储在 workspace/notes/notes.json，格式为 JSON 数组。

## 读取便签
```bash
cat {workspace}/notes/notes.json
```

## 创建便签
用 jq 追加到 JSON 数组：
```bash
jq '. += [{"id": "新ID", "content": "内容", "tags": [], "created_at": "ISO时间", "updated_at": "ISO时间"}]' {workspace}/notes/notes.json > tmp.json && mv tmp.json {workspace}/notes/notes.json
```

## 删除便签
用 jq 过滤掉指定 ID：
```bash
jq 'del(.[] | select(.id == "目标ID"))' {workspace}/notes/notes.json > tmp.json && mv tmp.json {workspace}/notes/notes.json
```

## 语义搜索
便签已自动同步到知识图谱，通过正常对话即可检索到相关便签内容。
```

### 3.5 删除清单

| 删除项 | 文件 | 说明 |
|--------|------|------|
| SQLite 数据层 | `niu_api/notes.py` | 整文件重写为 JSON |
| `aiosqlite` 依赖 | `niu_api/notes.py` | 不再需要 |
| `init_db()` 调用 | `niu_api/__main__.py` | 移除 notes DB 初始化 |
| `notes_router` 导入 | `niu_api/__main__.py` | 改为新模块导入 |
| `[Note:]` 前缀 | `lightrag_pipeline.py` | 移除 `source_type == "note"` 分支 |
| `sync_note_to_kg()` | `notes_api.py` | 重写为 LightRAGAdapter 调用 |
| `~/.niu/notes.db` | 运行时文件 | 不再创建 |

---

## 4. 前端兼容

前端 `sticky.html` 和 `main.js` 的 API 调用格式不变（`/api/notes` 端点保持相同），无需修改前端代码。

唯一变化：API 响应中每条便签多了 `tags` 字段。前端忽略不认识的字段，无影响。

---

## 5. 不做的事

- 不做数据迁移（便签从空开始）
- 不做启动时检测 `notes.db`
- 不改前端 UI 代码
- 不改 Electron 窗口管理代码
- 不改 memory-server 的 `user_memory_remember`（那是另一个系统）
