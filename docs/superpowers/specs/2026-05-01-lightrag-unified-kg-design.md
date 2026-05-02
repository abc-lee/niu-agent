# LightRAG 一统天下 — 知识管理架构重构设计

## 核心原则

1. **LightRAG 能做的，不要自己造轮子**
2. **程序能做的，不要返回给子Agent再问子Agent怎么办**
3. **文件整文件扔给 LightRAG，不要截断，不要逐条注入**

## 现状问题

我们装了 LightRAG，但还在自己搞：

| 自己搞的                              | LightRAG 已有                                               |
| --------------------------------- | --------------------------------------------------------- |
| `inject_entity` 直接操作 NetworkX 图   | `rag.create_entity()` / `rag.edit_entity()`               |
| `inject_relation` 直接操作 NetworkX 图 | `rag.create_relation()`                                   |
| `merge_persons` 手动迁移边+删节点         | `rag.merge_entities()`（内置多种合并策略）                          |
| 便签逐条 `inject_entity`              | `rag.ainsert(全文)` 自动提取实体和关系                               |
| 文档截断到 10000 字符                    | `rag.ainsert(全文)` 内部自动分块 1200 token                       |
| L0/L1/L2 三级存储                     | LightRAG 自动分块+实体提取+知识融合                                   |
| vector-store (ChromaDB)           | LightRAG 内置 entities_vdb + relationships_vdb + chunks_vdb |
| kg-server                         | LightRAG 内置实体/关系 CRUD                                     |
| 脑区激活只有衰减曲线，没有真正的图社区分区             | LightRAG 的 Leiden 社区检测 + 图邻居传播                            |

## 架构变更

### 删除

| 删除项 | 原因 |
|--------|------|
| `mcp-servers/vector-store/` | LightRAG 内置向量存储 |
| `mcp-servers/kg-server/` | LightRAG 内置实体/关系管理 |
| `agent/vector_search.py` | 改用 lightrag-server 的查询工具 |
| photo-server 的 `LightRAGIngester.inject_entity` 自实现 | 用 LightRAG 原生 API |
| photo-server 的 `LightRAGIngester.inject_relation` 自实现 | 用 LightRAG 原生 API |
| `store_document_l1` 工具 | 不再需要 L1 摘要 |
| L0/L1/L2 三级存储逻辑 | LightRAG 自动处理 |
| handler.py 中 vector-store/kg-server 的别名 | 服务器已删除 |

### 保留

| 保留项 | 原因 | 改动 |
|--------|------|------|
| photo-server | 照片管理+人脸识别 | KG 部分改用 LightRAG API |
| lightrag-server | 统一知识管理入口 | 无需改动（已用 hybrid 模式） |
| memory-server | 记忆管理独立于 KG | 无需改动 |
| config-manager | 配置管理 | 无需改动 |
| file-parser | 文件解析 | 无需改动 |
| session-manager | 会话管理 | 无需改动 |
| browser-server | 浏览器自动化 | 无需改动 |
| 虚拟磁盘工具 | 主Agent工具注入入口 | 不涉及 KG/向量库，兼容无影响 |

### 子Agent影响

| 子Agent | 影响 |
|---------|------|
| context-manager | 无影响（不用向量库） |
| dream-evolver | 无影响（已用 lightrag-server） |
| entity-extractor | 无影响（已用 lightrag-server） |
| event-manager | 无影响（不用向量库） |
| file-processor | 需改：ingest_document 不再返回 need_l1，全文入库 |

## 数据流设计

### 1. 文档入库

```
用户拖入文件
  → 主Agent直接传路径给子Agent（不分析类型）
  → 子Agent调用 ingest_document(path, mode="copy")
  → ingest_document 内部:
     1. 自动判断类型（目录/照片/文档）
     2. 如果是文档:
        a. 读文件内容（限制 <20K，超出截断）
        b. 返回内容给子Agent，问"这个文件放哪个目录？"
        c. 子Agent根据内容 + memory 中的分类偏好决定 category
        d. 子Agent再次调用 ingest_document(path, mode="copy", category="文档/技术")
        e. 文件搬运到目标目录
        f. rag.ainsert(全文) — LightRAG 自动分块+提取实体+建图
        g. 返回 success
     3. 如果是照片: 走照片入库流程
```

**关键变化**：
- 程序自动判定类型，不需要子Agent判断
- 程序读文档内容限制 <20K，拿这个内容去问子Agent放哪儿
- 全文传给 `ainsert`，不截断（LightRAG 内部自动分块）
- 不返回 `need_l1`（LightRAG 自动提取实体和关系）
- category 由子Agent决定（基于 memory 中的用户偏好）

### 2. 照片入库

```
用户拖入照片
  → 主Agent直接传路径给子Agent
  → 子Agent调用 ingest_document(path, mode="copy")
  → ingest_document 内部:
     1. 自动判断为照片
     2. 人脸识别
     3. 程序自动用默认 category（生活/工作/旅行/证件/其他）
        如果 memory 中有用户偏好，需要问子Agent确认
     4. 文件搬运到目标目录
     5. rag.insert_custom_kg(entities=[...], relations=[...])
        - 人物实体: entity_type="Person", description=名字, 不挂 file_path
        - 关系: depicts（照片→人物）
     6. 返回 success
```

**注意**：
- 照片文件本身不传给 `ainsert`（LightRAG 会 OCR，我们不需要）
- 照片的 category 有默认值，通常不需要问子Agent
- 但如果用户有特殊偏好（如"工作照片放工作目录"），需要子Agent决定

### 3. 便签入库

```
便签 JSON 文件变更
  → sync.py 检测到变更
  → rag.ainsert(JSON全文) — LightRAG 自动提取实体和关系
  → 不再逐条 inject_entity
```

**关键变化**：从逐条注入改为整文件 ainsert，让 LightRAG 自动重建图谱。

### 4. 人物改名

```
用户给人物命名/改名
  → rag.edit_entity(name=f"person:{id}", description=新名字)
  → 不再自己写 inject_entity
```

### 5. 人物合并

```
用户合并两个人物
  → rag.merge_entities(source=f"person:{b_id}", target=f"person:{a_id}")
  → 不再自己迁移边、删节点
```

### 6. 查询

```
用户提问
  → rag.aquery(query, mode="hybrid") — 图+向量双路检索
  → 已在 lightrag-server 中正确实现
```

### 7. 文件整理（辅助功能）

```
用户拖入文件（shift=move, ctrl=link）
  → 主Agent直接传路径给子Agent
  → 子Agent调用 ingest_document(path, mode="move/link")
  → ingest_document 判断类型，读内容(<20K)返回给子Agent
  → 子Agent根据内容 + memory 偏好决定 category
  → 文件搬运到目标目录
```

**这个流程不变**——category 必须由子Agent决定，因为需要读用户偏好记忆。

## 脑区激活机制

### 现状问题

当前脑区激活实现存在严重缺陷：

| 问题 | 严重性 | 说明 |
|------|--------|------|
| `neighbor_map={}` 空的 | HIGH | 激活不会向邻居脑区传播，溢出机制完全失效 |
| 默认脑区硬编码 | HIGH | 只有"聊天历史/文档库/知识体系"三个静态标签，不是 Leiden 检测的 |
| Leiden 结果未传递 | MEDIUM | 24h 周期太长，且结果没连到 neighbor_map |
| 激活后立即衰减 | MEDIUM | effective max=0.92 而非 1.0 |

**核心问题：脑区概念没有真正实现**——没有图社区分区，没有溢出传播，只是三个静态标签 + 衰减曲线。

### 改造方案

LightRAG 的 Leiden 社区检测 + 图结构天然支持脑区激活：

1. **分区**：LightRAG 的 `chunk_entity_relation_graph` 经 Leiden 检测自动产生社区（脑区）
2. **激活**：查询时 `aquery(mode="local")` 找到实体 → 沿边扩展 → 自动激活相关脑区
3. **溢出**：图邻居传播——激活一个节点，其邻居节点也获得部分激活
4. **衰减**：保留现有 `tool_lifecycle.py` 的衰减曲线，但激活源从向量检索改为 LightRAG 图检索
5. **再激活**：每轮 `_on_turn_end` 用当前上下文查询 LightRAG，命中脑区自动再激活

**具体实现**：
- `_inject_dynamic_resources` 改用 `rag.aquery(query, mode="local")` 替代 ChromaDB 向量检索
- 检索结果中包含实体和关联关系，天然实现脑区激活和溢出
- 衰减曲线保留，但激活分数来自 LightRAG 图检索的相关性评分

## 代码修改清单

### P0: photo-server KG 操作改用 LightRAG API

**文件**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

1. **删除 `LightRAGIngester.inject_entity` 自实现** → 改用 `rag.create_entity()` / `rag.edit_entity()`
2. **删除 `LightRAGIngester.inject_relation` 自实现** → 改用 `rag.create_relation()`
3. **`sync_photo_to_kg`** → 改用 `rag.insert_custom_kg()`
4. **`name_person`** → 改用 `rag.edit_entity(description=name)`
5. **`merge_persons`** → 改用 `rag.merge_entities()`
6. **`ingest_document`** → 全文 `rag.ainsert()`，不截断，不返回 need_l1
7. **`ingest_document`** → 自动判断类型，读内容 <20K 返回给子Agent问分类

### P1: 便签入库改为整文件 ainsert

**文件**: `agent/injector/sync.py`

1. **`_inject_note_to_lightrag`** → 从逐条 inject_entity 改为 `rag.ainsert(JSON全文)`

### P2: 脑区激活改用 LightRAG 图检索

**文件**: `agent/generic/runner.py`

1. **`_inject_dynamic_resources`** → 从 ChromaDB 向量检索改为 `rag.aquery(mode="local")`
2. **`_on_turn_end`** → 保留衰减曲线，激活源改为 LightRAG 图检索评分

### P3: 删除废弃服务器

1. **删除 `mcp-servers/vector-store/`**
2. **删除 `mcp-servers/kg-server/`**
3. **删除 `agent/vector_search.py`**
4. **清理 `config/mcp-servers.yaml`** 中的 vector-store/kg-server 配置
5. **清理 `handler.py`** 中的 vector-store/kg-server 别名
6. **清理 `runner.py`** 中对 vector_search 的引用

### P4: 子Agent提示词更新

1. **`config/agents/file-processor.md`** — 简化文档处理流程，ingest_document 不再返回 need_l1

## 风险评估

| 风险 | 等级 | 对策 |
|------|------|------|
| LightRAG 单点故障 | 低 | 已经是单点（vector-store 也用它），风险未增加 |
| RC 版本 breaking changes | 低 | 核心 API 稳定，关注 changelog |
| 旧数据迁移 | 中 | 一次性脚本：从旧存储导出 → rag.ainsert 重建 |
| merge_entities 行为差异 | 中 | 需测试 LightRAG 的合并策略是否符合预期 |
| 大文件 ainsert 性能 | 低 | LightRAG 内部异步分块处理 |
| 虚拟磁盘兼容性 | 无 | 虚拟磁盘不涉及 KG/向量库，不受影响 |

## 版本兼容性

### 当前版本

- 安装版本：`lightrag-hku==1.4.15`
- 安装路径：`E:\opencode\venv\Lib\site-packages\lightrag\`
- 核心 API（ainsert/aquery/实体管理）稳定，无 breaking changes

### Fork 修复状态

我们 fork（abc-lee/LightRAG）的 2 个修复：

| 修复 | 位置 | 内容 |
|------|------|------|
| `_merge_nodes_then_upsert` | operate.py ~L1905 | 合并节点时保留已有自定义属性（如 brain_meta_*） |
| `_merge_edges_then_upsert` | operate.py ~L2434 | 合并边时保留已有自定义属性 |

- 修复已提交为上游 PR #2990（2026-04-27），**目前状态：OPEN，尚未合并**
- 当前 pip 安装的 v1.4.15 已包含修复（直接修改了 site-packages 文件）
- 项目中没有单独的 monkey-patch 代码

### 升级风险

| 操作 | 风险 | 对策 |
|------|------|------|
| `pip install --upgrade lightrag-hku` | **高** — 上游未合并 PR #2990，升级会丢失属性保留修复 | 等 PR #2990 合并后再升级 |
| LightRAG RC/正式版 API 变更 | 低 — 核心 API 稳定 | 关注 changelog |
| 存储后端变更 | 低 — 抽象化方向不影响上层 | 无需额外处理 |

**结论：当前版本对改造方案无影响。升级需等 PR #2990 合并。**
