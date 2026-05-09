# Photo KG Structured Injection — Test-Before-Fix Design

> **核心原则：不动现有代码，先写测试证明方案可行，用户审查通过后才动代码。**
> **最终目标：形成完整的图谱开发字典，后期所有开发直接查字典，无需再测试。**

## 最终交付物

**图谱开发字典** (`docs/kg-dev-dictionary.md`)，覆盖以下操作的完整规范：

| 操作 | 字典内容 |
|------|---------|
| 文档入库 | 调用哪个接口、传什么参数、source_id 格式、chunk 结构 |
| 照片入库 | entity_name 格式、file_path 必传、多人同框关系、与脑区连接 |
| 人物命名 | 如何更新 person:{uuid} 描述、不触发 LLM |
| 人物合并 | merge 流程、边迁移、旧实体删除 |
| 图谱查询 | 查实体、查关系、查邻居、全文检索 |
| 实体增删 | 新增实体、删除实体、更新属性 |
| 脑区管理 | 脑区节点格式、与实体连接规则 |
| 脑区增删合并 | 新建脑区、删除脑区、合并脑区（边迁移） |
| 内容提取入库 | 聊天记录→精炼文档→入库、避免重复实体 |
| 梦境进化 | 精加工流程、打标签、建关系、关联脑区 |

每项操作包含：**接口名 → 参数模板 → 返回值 → 注意事项 → 已知陷阱**

## 问题根因

`sync_photo_to_kg` 使用 `lightrag_insert`（ainsert，LLM 自动提取），导致：

1. 照片实体无 `file_path` 属性 → 前端 `orig.uri` 为空 → 详细页无法显示照片缩略图
2. LLM 二次提取时创建重复 photo/person 实体（如同时创建 `person:uuid` 和 "张三"）
3. 关系被兜底挂到 `brain:Niu`，而非正确的 `person:{uuid}` / `photo:{file_path}`

## 修复方案

将 `sync_photo_to_kg`、`name_person`、`merge_persons` 中的 LLM 提取路径改为 `lightrag_insert_custom_kg`（结构化注入），完全绕过 LLM 自动提取。

## 测试策略

### 环境要求

- 与生产环境完全一致：启动 LightRAG 服务 + LLM 代理
- 使用独立的测试图谱工作目录（`~/.niu/lightrag_test/`），不污染生产数据
- 测试完成后可清理

### 双 Agent 架构

```
┌─────────────────┐     ┌─────────────────┐
│  Ingestion Agent │     │  Review Agent   │
│  (入库 Agent)    │     │  (审查 Agent)   │
├─────────────────┤     ├─────────────────┤
│ 1. 初始化测试环境 │     │ 1. 查询图谱状态 │
│ 2. 注入照片实体   │     │ 2. 验证实体数量 │
│ 3. 注入人物实体   │     │ 3. 验证file_path│
│ 4. 建立关系      │     │ 4. 验证关系目标 │
│ 5. 模拟name_person│    │ 5. 验证无重复   │
│ 6. 模拟梦境整合   │     │ 6. 验证前端映射 │
│ 7. 输出操作日志   │     │ 7. 输出审查报告 │
└─────────────────┘     └─────────────────┘
         │                       │
         └─────── 共享图谱 ───────┘
```

### 测试流程（8 个阶段）

#### 阶段 1：环境初始化

- 创建测试专用 LightRAG 工作目录
- 初始化 LightRAG 实例（使用生产配置的 LLM 代理）
- 注入 `brain:Niu` 根节点和脑区节点

#### 阶段 2：照片入库（Ingestion Agent）

模拟 `sync_photo_to_kg` 的**新实现**：

```python
# 结构化注入（新方案）
lightrag_insert_custom_kg(
    entities=[
        {
            "entity_name": f"photo:{file_path}",
            "entity_type": "Photo",
            "description": abstract,
            "file_path": file_path,  # 关键：让前端 orig.uri 有值
        },
        # 每个检测到的人物
        {
            "entity_name": f"person:{person_id}",
            "entity_type": "Person",
            "description": person_name or "未命名人物",
        },
    ],
    relationships=[
        # 照片→人物关系
        {
            "src_id": f"photo:{file_path}",
            "tgt_id": f"person:{person_id}",
            "keywords": "features",
            "description": f"照片中出现了{person_name or '未命名人物'}",
        },
        # 人物→脑区根节点
        {
            "src_id": "brain:Niu",
            "tgt_id": f"person:{person_id}",
            "keywords": "remembers",
            "description": f"认识{person_name or '未命名人物'}",
        },
        # 照片→脑区根节点
        {
            "src_id": "brain:Niu",
            "tgt_id": f"photo:{file_path}",
            "keywords": "remembers",
            "description": "拥有这张照片",
        },
    ],
    chunks=[
        {
            "content": f"照片 {Path(file_path).stem}: {abstract}",
            "source_id": f"photo:{file_path}",
            "file_path": file_path,
        },
    ],
    source_id=f"photo:{file_path}",
)
```

#### 阶段 3：多人同框（Ingestion Agent）

模拟一张照片中检测到**多个**人物的真实场景：

- 注入一张含 3 个人的照片（如家庭合影）
- 每个人物创建 `person:{uuid}` 实体
- 照片与每个人物建立 `features` 关系
- **人物之间**建立 `co_occurs_with` 关系（同框关系）
- 每个人物与 `brain:Niu` 建立 `remembers` 关系

```python
# 多人同框：人物间同框关系
for i, person_a in enumerate(detected_persons):
    for person_b in detected_persons[i+1:]:
        relationships.append({
            "src_id": f"person:{person_a['id']}",
            "tgt_id": f"person:{person_b['id']}",
            "keywords": "co_occurs_with",
            "description": f"{person_a['name']}和{person_b['name']}在同一张照片中出现",
        })
```

**验证点**：
- 同框关系是否正确建立（A↔B、A↔C、B↔C）
- 同框关系不会误挂到 `brain:Niu`
- 多人场景下不会产生重复实体

#### 阶段 4：人物命名（Ingestion Agent）

模拟 `name_person` 的**新实现**：

```python
lightrag_insert_custom_kg(
    entities=[
        {
            "entity_name": f"person:{person_id}",
            "entity_type": "Person",
            "description": real_name,  # 更新描述为真实姓名
        },
    ],
    relationships=[
        {
            "src_id": "brain:Niu",
            "tgt_id": f"person:{person_id}",
            "keywords": "remembers",
            "description": f"认识{real_name}",
        },
    ],
    chunks=[],
    source_id=f"person:{person_id}",
)
```

#### 阶段 5：人物合并（Ingestion Agent）

模拟 `merge_persons` 的**新实现**（两张照片中同一个人被识别为两个 person）：

```python
# 1. 用 lightrag_insert_custom_kg 更新合并后实体的描述
lightrag_insert_custom_kg(
    entities=[{
        "entity_name": f"person:{person_a_id}",
        "entity_type": "Person",
        "description": merged_name,
    }],
    relationships=[{
        "src_id": "brain:Niu",
        "tgt_id": f"person:{person_a_id}",
        "keywords": "remembers",
        "description": f"认识{merged_name}",
    }],
    chunks=[],
)

# 2. 用 lightrag_merge_entities 迁移 person_b 的所有边到 person_a，然后删除 person_b
lightrag_merge_entities(
    source_entities=[f"person:{person_b_id}"],
    target_entity=f"person:{person_a_id}",
)
```

**验证点**：
- 合并后只存在 `person:{person_a_id}`，`person:{person_b_id}` 已删除
- person_b 的所有关系（包括同框关系、照片 features 关系）已迁移到 person_a
- 合并过程不会触发 LLM 创建独立人名实体
- 合并后同框关系仍然正确（person_a 与第三人的关系保留）

#### 阶段 6：真实聊天记录内容提取重新入库（Ingestion Agent）

**使用生产环境中已有的真实聊天记录**，完整模拟 entity-extractor → dream-evolver 流程：

1. **读取真实聊天记录**：从 session-manager 获取最近的聊天消息
2. **模拟 entity-extractor**：将聊天内容格式化为精炼文档，调用 `lightrag_insert(content=精炼文档)` — 这会触发 LLM 自动提取实体和关系
3. **模拟 dream-evolver**：搜索本次提取涉及的实体，用 `lightrag_insert_entity` / `lightrag_insert_relation` 做精加工（打标签、建关系、关联脑区）

**关键验证**：
- 内容提取后，LLM 不会创建与已有 `photo:{fp}` / `person:{uuid}` 重复的实体
- 如果聊天中提到"张三"，LLM 应该关联到已有的 `person:{uuid}` 实体，而不是创建独立的"张三"实体
- 如果聊天中提到照片，LLM 应该关联到已有的 `photo:{fp}` 实体，而不是创建新的照片实体
- brain_region_prompt 的规则是否有效引导 LLM 合并

#### 阶段 7：图谱审查（Review Agent）

审查 Agent 执行以下检查：

| 检查项 | 预期结果 | 验证方法 |
|--------|---------|---------|
| 照片实体数量 | 恰好 N 个 `photo:{file_path}` | `lightrag_list_entities(entity_type="Photo")` |
| 人物实体数量 | 恰好 M 个 `person:{uuid}` | `lightrag_list_entities(entity_type="Person")` |
| 照片 file_path | 等于实际文件路径 | 查询实体详情，检查 `file_path` 属性 |
| 照片→人物关系 | 存在 `photo:{fp} --features--> person:{uuid}` | `lightrag_get_graph(action="explore", entity_name="photo:{fp}")` |
| 人物→根节点关系 | 存在 `brain:Niu --remembers--> person:{uuid}` | `lightrag_get_graph(action="explore", entity_name="person:{uuid}")` |
| 同框关系 | 存在 `person:{a} --co_occurs_with--> person:{b}` | `lightrag_get_graph(action="explore")` 检查边 |
| 合并后旧实体不存在 | `person:{person_b_id}` 已删除 | `lightrag_get_graph(action="explore", entity_name="person:{person_b_id}")` 返回空 |
| 合并后关系已迁移 | person_b 的边全部迁移到 person_a | 遍历 person_a 的所有边 |
| 无重复实体 | 不存在自然语言命名的照片/人物实体（如"照片"、"张三"独立于 person:uuid） | `lightrag_list_entities()` 检查 |
| 前端映射正确 | `orig.uri` = `file_path` | 查询 changelog，验证 `file_path` 字段 |
| 内容提取后无重复 | 聊天内容提取不会创建与已有 photo/person 重复的实体 | 对比提取前后的实体列表 |

#### 阶段 8：对比测试（旧方案 vs 新方案）

在同一个测试环境中，分别用两种方案注入照片，对比结果：

| 对比项 | 旧方案（lightrag_insert） | 新方案（lightrag_insert_custom_kg） |
|--------|--------------------------|-----------------------------------|
| 照片实体 file_path | "unknown_source" 或空 | 实际文件路径 |
| 重复实体数 | >0（LLM 可能创建） | 0（结构化注入不触发 LLM） |
| 关系目标 | 可能挂到 brain:Niu | 正确挂到 person:{uuid} |
| 同框关系 | 可能丢失或挂错 | 正确建立 person↔person |
| 合并后残留 | 可能残留旧实体边 | 干净迁移 |
| 前端能否显示缩略图 | 不能（orig.uri 为空） | 能（orig.uri = file_path） |

### 测试程序结构

```
scripts/test_photo_kg_structured.py    # 主测试脚本
├── TestEnvironment                     # 环境管理（初始化/清理）
├── IngestionAgent                     # 入库 Agent
│   ├── ingest_photo_structured()      # 结构化注入照片（含多人同框）
│   ├── name_person_structured()       # 结构化更新人物
│   ├── merge_persons_structured()     # 结构化合并人物
│   ├── extract_chat_content()         # 真实聊天记录内容提取入库
│   └── simulate_dream_evolution()     # 模拟梦境进化精加工
├── ReviewAgent                        # 审查 Agent
│   ├── verify_entity_count()          # 验证实体数量
│   ├── verify_file_path()             # 验证 file_path 属性
│   ├── verify_relationships()         # 验证关系目标（含同框关系）
│   ├── verify_merge_result()          # 验证合并后旧实体删除+边迁移
│   ├── verify_no_duplicates()         # 验证无重复实体
│   └── verify_frontend_mapping()      # 验证前端映射
└── ComparisonTest                     # 对比测试
    ├── test_old_approach()            # 旧方案测试
    ├── test_new_approach()            # 新方案测试
    └── compare_results()              # 对比结果
```

### 成功标准

测试通过的条件：

1. 新方案注入后，照片实体 `file_path` 等于实际文件路径
2. 新方案注入后，不存在重复的 photo/person 实体
3. 新方案注入后，照片→人物关系指向正确的 `person:{uuid}`
4. 多人同框时，人物间 `co_occurs_with` 关系正确建立，不误挂到 brain:Niu
5. 人物合并后，旧实体删除干净，所有边迁移到合并后实体，同框关系保留
6. 真实聊天记录内容提取后，LLM 不会创建与已有 `photo:{fp}` / `person:{uuid}` 重复的实体
7. 前端 `orig.uri` 映射正确，能显示照片缩略图
8. 对比测试：新方案在所有检查项上优于旧方案

### 执行方式

```bash
# 运行完整测试（需要 LightRAG 服务已启动）
python scripts/test_photo_kg_structured.py

# 仅运行新方案测试
python scripts/test_photo_kg_structured.py --new-only

# 仅运行对比测试
python scripts/test_photo_kg_structured.py --compare

# 清理测试数据
python scripts/test_photo_kg_structured.py --cleanup
```

## 修改清单（测试通过后才执行）

| 文件 | 函数 | 当前 | 改为 |
|------|------|------|------|
| `mcp-servers/photo-server/.../__init__.py:459` | `sync_photo_to_kg` | `lightrag_insert` | `lightrag_insert_custom_kg` |
| `mcp-servers/photo-server/.../__init__.py:1805` | `name_person` | `lightrag_insert_entity` | `lightrag_insert_custom_kg` |
| `mcp-servers/photo-server/.../__init__.py:2028` | `merge_persons` | `lightrag_insert_entity` | `lightrag_insert_custom_kg` |
