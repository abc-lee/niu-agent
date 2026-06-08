# 图片相关代码问题清单（审核后）

> **⚠️ 历史文档**：本文档中使用 `brain:Niu`、`brain:region:xxx`、`brain:concept:xxx`、`brain:event:xxx`、`brain:person:xxx`、`brain:session:xxx`、`event:xxx`、`skill:xxx`、`person:xxx` 等冒号前缀实体名的描述已过时。当前系统要求所有实体名必须使用自然语言（如 `Niu`、`编程开发脑区`、`Python`、`海滩日落事件`），禁止冒号前缀格式。详见 `docs/kg-dev-dictionary.md`。

> 分析Agent发现 → 审核Agent逐条验证 → 标记最终判定

## 判定汇总

| 判定 | 数量 | 问题ID |
|------|------|--------|
| CONFIRMED | 10 | C1, C3, H1, H3, H5, H7, M1, M2, M4, M7 |
| REJECTED | 5 | H4, H6, H8, M3, M6 |
| MODIFIED | 4 | C2, H2, M5, M7 |

---

## CRITICAL 级别

### C1: sync_photo_to_kg 未传递 file_path 参数 — CONFIRMED
- **位置**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` L536
- **描述**: `sync_photo_to_kg` 调用 `lightrag_insert_custom_kg` 时未传递 `file_path` 参数
- **审核证据**: 代码中 L536 调用 `lightrag_insert_custom_kg(entities=entities, relationships=relationships)` 无 file_path 参数
- **影响**: 所有照片文档的 source_id 为 unknown_source，无法追溯来源
- **修复**: 添加 `file_path=source_path.replace("\\", "/")`

### C2: lightrag_insert_entity 无模糊去重能力 — MODIFIED
- **位置**: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` L899+
- **原描述**: "无去重机制"
- **修正**: 不是完全无去重——LightRAG 的 ainsert 有精确同名合并机制（同名实体合并描述），但**无模糊去重能力**（如 person:{uuid} 和"任飞"语义相同但名称不同，无法合并）
- **审核证据**: ainsert 内部有同名合并逻辑，但只匹配精确名称
- **影响**: 语义相同但名称不同的实体仍会碎片化
- **修复**: 在 lightrag_insert_entity 中增加别名查询机制，或改用 ainsert_custom_kg

### C3: lightrag_merge_entities 精确匹配静默失败 — CONFIRMED
- **位置**: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` L475+
- **描述**: 使用精确名称匹配，大小写不一致时合并静默失败无任何提示
- **审核证据**: 函数使用 `_entity_name_to_id` 精确查找，找不到时直接返回，无日志
- **影响**: name_person 调用 merge 时可能因大小写不一致而合并失败
- **修复**: 精确匹配失败时添加 logger.warning，并尝试大小写不敏感匹配

---

## HIGH 级别

### H1: person:{uuid} 实体命名导致碎片化 — CONFIRMED
- **位置**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` format_photo_ingest_data L432-508
- **描述**: 照片实体使用 `person:{uuid}` 格式命名，LLM 提取时可能同时创建人名实体
- **审核证据**: L455 `person_entity_name = f"person:{person_id}"`，LLM 无法将此与人名关联
- **影响**: 同一人物出现两个独立实体节点
- **修复**: 使用 auto_label 作为实体名，person:{uuid} 作为属性/别名

### H2: name_person 不处理 KG 中已存在的独立人名实体 — MODIFIED
- **位置**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` name_person L1795-1933
- **原描述**: "不合并 KG 中的已有实体"
- **修正**: name_person 确实调用了 merge_entities 合并 person:{uuid}→人名，但如果 KG 中已存在由 ainsert 创建的独立人名实体（如"任飞"），**不会执行三方合并**（person:{uuid} + ainsert创建的"任飞" → 合并为一个）
- **审核证据**: name_person L1904-1928 只做 person:{uuid}→人名的合并，不查询 KG 中是否已有同名独立实体
- **影响**: 命名后 KG 中仍可能存在碎片化（person:{uuid} 已合并为人名，但 ainsert 创建的独立人名实体仍存在）
- **修复**: name_person 合并前先查询 KG 中是否已有同名实体，如有则执行三方合并

### H3: lightrag_insert_relation 不验证实体存在性 — CONFIRMED（MODIFIED描述）
- **位置**: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` L942+
- **原描述**: "直接插入关系，不验证源/目标实体是否存在于图谱中，产生孤立关系边"
- **修正**: 使用 ainsert 插入关系时，LightRAG 会自动创建缺失的端点实体（不是孤立边）。但风险是 LLM 可能以不一致的名称创建端点实体，导致碎片化
- **审核证据**: ainsert 会自动创建端点实体，不会产生孤立边
- **影响**: LLM 创建不一致名称的端点实体，导致碎片化
- **修复**: 改用 ainsert_custom_kg 插入关系，或插入前验证实体存在

### H4: lightrag_insert_custom_kg 不跟踪实体创建状态 — REJECTED
- **审核证据**: 函数实际返回了完整结果，包含新增/更新实体信息
- **理由**: 描述不属实，函数已有返回值机制

### H5: format_photo_ingest_data 路径归一化不完整 — CONFIRMED
- **位置**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` L440
- **描述**: 虽然已添加 `file_path.replace("\\", "/").lower()` 归一化 photo 实体名，但 source_id 等其他位置可能未同步归一化
- **审核证据**: photo_entity_name 已归一化，但需检查所有引用点是否一致
- **影响**: 同一照片可能因路径格式不同创建多个实体
- **修复**: 确保所有使用 file_path 构造实体名/关系的地方都使用归一化后的路径

### H6: ingest_photo 中 photo 实体名可能不一致 — REJECTED
- **审核证据**: ingest_photo 只通过 sync_photo_to_kg → format_photo_ingest_data 一个路径构造 photo 实体名，不存在多处构造不一致的问题
- **理由**: 描述不属实

### H7: lightrag_insert_entity 用 ainsert 而非 ainsert_custom_kg — CONFIRMED
- **位置**: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` L899+
- **描述**: `lightrag_insert_entity` 使用 `ainsert`（LLM 提取），LLM 可能创建额外实体
- **审核证据**: 代码确认使用 ainsert
- **影响**: 每次插入实体时 LLM 可能创建不期望的额外实体
- **修复**: 改用 ainsert_custom_kg 或增加别名查询机制

### H8: brain_region_prompt 动态查询可能触发无限循环 — REJECTED
- **审核证据**: `only_need_context=True` + `mode="local"` 明确阻止 LLM 调用，不会形成循环
- **理由**: 描述不属实

---

## MEDIUM 级别

### M1: 实体名称大小写未归一化 — CONFIRMED
- **位置**: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` 全局
- **描述**: LLM 提取实体时大小写不统一（brain:Niu vs Brain:Niu）
- **审核证据**: 无大小写归一化机制
- **影响**: 同一概念因大小写不同创建多个实体
- **修复**: 在 lightrag_insert_entity 中归一化已知前缀（brain:, person:, photo:）

### M2: file_path 默认值为 custom_kg — MODIFIED
- **位置**: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py`
- **原描述**: "默认值为 unknown_source"
- **修正**: 默认值是 `"custom_kg"` 而非 `"unknown_source"`。但 `lightrag_insert`（ainsert路径）的 file_path 默认值确实为 `"unknown_source"`，而 `lightrag_insert_custom_kg` 的默认值为 `"custom_kg"`
- **影响**: ainsert 路径的文档来源不可追溯
- **修复**: ainsert 路径应要求调用方提供 file_path

### M3: sync 模块中照片同步逻辑重复 — REJECTED
- **审核证据**: lightrag_sync.py 文档明确说明"Photos are NOT synced here"，两个模块职责不同（KG同步 vs Skills同步）
- **理由**: 描述不属实

### M4: lightrag_insert_entity 的 skip_llm_extraction 参数被忽略 — CONFIRMED（升级为HIGH）
- **位置**: `mcp-servers/lightrag-server/src/niu_lightrag_server/__init__.py` L905, L918
- **描述**: `skip_llm_extraction` 参数在函数签名中存在但被显式丢弃（`_ = source_id, skip_llm_extraction`）
- **审核证据**: L905 `skip_llm_extraction: bool = False` # deprecated, always uses ainsert now; L918 `_ = source_id, skip_llm_extraction`
- **影响**: 调用方传入 `skip_llm_extraction=True` 期望跳过 LLM 提取，但实际始终使用 ainsert
- **修复**: 要么移除此参数避免误导，要么实现其功能

### M5: merge_persons 合并后可能产生重复边 — MODIFIED
- **位置**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` merge_persons L~1935-2010
- **原描述**: "不更新 KG 中的关系"
- **修正**: merge_persons 在 KG 层确实通过 lightrag_merge_entities 更新了关系（amerge_entities 迁移边），但如果两个源实体都与同一第三方实体有同类关系，合并后可能出现**重复边**
- **影响**: 合并后同一关系类型出现多条边
- **修复**: 合并后检查并去重重复边

### M6: co_occurrence 关系未同步到 KG — REJECTED
- **审核证据**: format_photo_ingest_data L499-508 已将 co_occurs_with 关系注入 KG
- **理由**: 描述不属实

### M7: 照片 abstract 包含临时命名 — CONFIRMED
- **位置**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` generate_l0_abstract L1568-1586
- **描述**: abstract 中包含"未命名人物_1"等临时命名，人物命名后 abstract 未更新
- **审核证据**: generate_l0_abstract 使用 person_names（来自 detected_persons 的 auto_label）
- **影响**: KG 中的照片描述仍使用临时命名
- **修复**: name_person 后更新相关照片的 abstract 和 KG 中的描述

---

## 待修复问题优先级排序

### 阶段1: CRITICAL（阻止数据损坏）
1. **C1**: sync_photo_to_kg 添加 file_path 参数
2. **C2**: lightrag_insert_entity 增加别名查询/去重机制
3. **C3**: lightrag_merge_entities 精确匹配失败时添加日志+大小写不敏感匹配

### 阶段2: HIGH（防止持续碎片化）
4. **H1**: person:{uuid} 改为基于人名的实体命名
5. **H2**: name_person 增加三方合并逻辑
6. **H7**: lightrag_insert_entity 改用 ainsert_custom_kg 或增加控制
7. **M4→H**: skip_llm_extraction 参数要么移除要么实现
8. **H5**: 确保所有路径引用点使用归一化路径

### 阶段3: MEDIUM（数据质量）
9. **M1**: 实体名称大小写归一化
10. **M2**: ainsert 路径的 file_path 默认值改进
11. **M5**: merge_persons 合并后去重重复边
12. **M7**: name_person 后更新照片 abstract