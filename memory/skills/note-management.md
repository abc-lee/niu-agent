---
name: note-management
description: Use when user asks to create, read, update, or delete sticky notes, or when user mentions notes, 便签, or reminders
---

# Note Management

## Overview

便签（sticky notes）通过 REST API 管理，存储在 `{workspace}/notes/notes.json`。便签自动同步到 LightRAG 知识图谱，供图检索使用。

## Quick Start

```bash
# 查看所有便签
curl http://localhost:9876/api/notes

# 创建便签
curl -X POST http://localhost:9876/api/notes \
  -H "Content-Type: application/json" \
  -d '{"id": "shopping", "content": "买牛奶和鸡蛋", "tags": ["购物"], "createdAt": 1700000000000}'
```

## Core Operations

### 创建便签

```
POST /api/notes
Body: {"id": "<note_id>", "content": "<text>", "tags": ["tag1"], "createdAt": <ms_timestamp>}
```

- `id`: 便签唯一标识（字符串）
- `content`: 便签内容
- `tags`: 标签列表（可选）
- `createdAt`: 创建时间（前端传 ms 时间戳）

### 查看便签

```
GET /api/notes          # 列出所有便签
GET /api/notes/<id>     # 查看单个便签
```

### 更新便签

```
PUT /api/notes/<id>
Body: {"id": "<note_id>", "content": "<new_text>", "tags": ["tag1", "tag2"], "updatedAt": <ms_timestamp>}
```

### 删除便签

```
DELETE /api/notes/<id>
```

删除便签时会自动从 LightRAG 知识图谱中移除对应实体。

## Storage

便签存储在 `{WORKSPACE_PATH}/notes/notes.json`，使用原子写入（temp file + os.replace）确保数据安全。

## LightRAG Sync

- 创建/更新便签时，后台任务自动将便签注入 LightRAG（`entity_type="knowledge"`，`name="note:{id}"`）
- SkillSync 定时扫描 `notes.json`，检测内容变化并同步
- 删除便签时自动从 LightRAG 删除对应实体