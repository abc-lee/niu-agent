# 知识图谱数据流入规划

## 问题

知识图谱（KuzuDB）有完整的读写能力（18个工具+9个API端点），但**没有任何生产代码路径将数据写入**。所有数据渠道（文档、照片、聊天、便利贴）都绕过了KG，各自写自己的数据库。KG是空壳。

## 现状：5个数据孤岛

| 数据库 | 存什么 | 谁写入 | KG有？ |
|--------|--------|--------|--------|
| `vectors.db` | L1摘要+向量 | `store_document_l1` | ❌ |
| `photos.db` | 照片+人脸+人物+同框 | `ingest_photo` | ❌ |
| `messages.db` | 聊天消息 | session模块 | ❌ |
| `knowledge.db` | 实体+文档+概念+关系 | **无人调用** | ✅（空） |
| 便利贴存储 | 便利贴内容 | UI → `/api/notes` | ❌ |

## 规划：5条数据流入渠道

### 渠道1：文档入库 → KG

**触发**：`store_document_l1` 完成后（向量写入成功后）

**操作**：
1. 调 `kg-server/create_document`（uri=文件路径, title=文件名, source=入库来源）
2. 从 L1 摘要中提取实体（L1 格式：`标题|关键词|摘要|实体|类型|指针`，实体字段已有）
3. 对每个实体调 `kg-server/create_entity`（type=实体类型, confidence=0.7）
4. 调 `kg-server/link_document_entity`（confidence=0.7）

**实现位置**：在 `photo-server/store_document_l1` 和 `store_documents_l1` 末尾添加 KG 写入逻辑

**置信度**：LLM提取=0.7

---

### 渠道2：照片入库 → KG

**触发**：`ingest_photo` 完成后（人脸识别完成后）

**操作**：
1. 调 `kg-server/create_document`（uri=照片路径, title=文件名, source="photo"）
2. 对每个检测到的人物调 `kg-server/create_entity`（type="person", name=人物名或"未知人物", confidence=0.8）
3. 调 `kg-server/link_document_entity`（照片 MENTIONS 人物, confidence=0.8）
4. 同框人物之间调 `kg-server/link_entities`（relation="co_occurs_with", confidence=0.6）

**实现位置**：在 `photo-server/ingest_photo` 末尾添加 KG 写入逻辑

**注意**：当前 `photos.db` 中已有同框关系（`co_occurrences` 表），需同步到 KG

**置信度**：人脸检测=0.8，同框推断=0.6

---

### 渠道3：聊天对话 → KG

**触发**：每轮对话结束后（`tool_after_callback` 或新回调）

**操作**：
1. 从当前轮次的用户消息+AI回复中提取实体和概念
2. 对每个实体调 `kg-server/create_entity`（confidence=0.5）
3. 对每个概念调 `kg-server/create_concept`（confidence=0.5）
4. 建立实体间关系 `link_entities`（confidence=0.4）

**实现位置**：`agent/handler.py` 的 `tool_after_callback` 中添加 KG 提取逻辑

**关键问题**：实体提取用什么方式？
- 方案A：LLM提取（每轮额外调一次LLM，成本高但准确）
- 方案B：规则提取（正则匹配人名/地名/组织名，快但不准）
- 方案C：混合（规则粗筛 + LLM精筛，仅对规则命中的内容调LLM）

**置信度**：Agent推断=0.4-0.6

---

### 渠道4：便利贴 → KG

**触发**：创建/编辑便利贴时

**操作**：
1. 便利贴内容作为 Concept 节点写入 KG
2. 从内容中提取实体，调 `create_entity` + `link_document_entity`
3. 自动关联当前对话上下文中的实体

**实现位置**：便利贴保存逻辑中添加 KG 写入（待确认便利贴存储位置）

**置信度**：用户手动=1.0

---

### 渠道5：定期批量整理

**触发**：定时任务（类似向量库的 SkillSync）

**操作**：
1. **共现分析**：扫描所有 MENTIONS 边，发现同文档的实体对，补建 RELATED_TO 边
2. **关系强度更新**：重新计算 confidence（基于共现次数、时间衰减）
3. **孤立节点清理**：删除无任何边的节点
4. **向量库→KG同步**：扫描 vectors.db 中有向量但 KG 中无对应 Document 的记录，补建 KG 节点
5. **photos.db→KG同步**：扫描 photos.db 中有人物但 KG 中无对应 Entity 的记录，补建 KG 节点

**实现位置**：新增 `niu_api/internal/kg_sync.py`，由调度器定时触发

**频率**：每小时一次（可配置）

---

## 实施优先级

1. **渠道1**（文档→KG）— 最直接，L1 摘要已有实体信息
2. **渠道2**（照片→KG）— photos.db 已有数据，只需桥接
3. **渠道5**（批量整理）— 补历史数据+持续维护
4. **渠道3**（聊天→KG）— 需要设计实体提取策略
5. **渠道4**（便利贴→KG）— 需要确认便利贴存储位置

## 实施状态（2026-04-15）

- [x] **渠道1**（文档→KG）— 已实现：`sync_to_kg()` + 接入 `store_document_l1`/`store_documents_l1`
- [x] **渠道2**（照片→KG）— 已实现：`sync_photo_to_kg()` + 接入 `ingest_photo`
- [x] **渠道5**（批量整理）— 已实现：`agent/injector/kg_sync.py` KGSync 服务，6小时周期
- [ ] **渠道3**（聊天→KG）— 待"梦境进化"子Agent方案，与内容整理Agent分工
- [ ] **渠道4**（便利贴→KG）— 待确认便利贴存储位置 + 梦境进化方案

**渠道3/4 方向**：聊天→KG 不在主Agent回调中实现，而是由独立的"梦境进化"子Agent负责。该子Agent定期分析对话内容，提取实体/概念/关系写入KG，同时完成内容整理工作。这样主Agent保持轻量，KG提取有独立上下文和LLM调用能力。

## 待确认事项

- [ ] 便利贴的存储位置和 API 实现（前端调 `/api/notes` 但后端无对应路由）
- [ ] KG 中现有 person 实体的来源（LLM手动创建？还是某个未发现的代码路径？）
- [ ] 渠道3/4 合并到"梦境进化"子Agent方案设计
- [x] `tool-layer-decision.md` 中 kg-server 被标记为"不暴露给Agent"，已更新标注程序化调用

## 执行方式

采用 **渐进式实施**：从渠道1开始，每条渠道独立实现+测试+验证，再进入下一条。每条渠道完成后验证图谱可视化能看到新数据。
