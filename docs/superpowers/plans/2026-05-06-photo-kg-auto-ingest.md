# 照片知识图谱自动入库方案

> 日期: 2026-05-07
> 状态: TESTED ✅ → 实施阶段
> 目标: 用 LightRAG `ainsert` 替代手动构建实体/关系，解决"任飞"实体重复与断连问题

---

## 0. 测试验证结果

### 基础测试（T1-T7）

| 测试 | 结果 | 关键发现 |
|------|------|----------|
| T1 | ✅ | ainsert 成功提取"任飞"、"北京颐和园"等实体 |
| T2 | ✅ | 同名实体自动合并（`Merged: '任飞' \| 1+1`） |
| T3 | ✅ | UUID-真名自动合并（`Merged: 'person-test-002'~'王芳' \| 1+1`） |
| T4 | ✅ | 多照片同人物，"任飞"实体只有1个 |
| T5 | ✅ | 信息补充后正确关联 |
| T6 | ✅ | 梦境整理路径可行 |
| T7 | ✅ | 删除功能正常 |

### 更名测试（T8: 多照片同一UUID→更名→后续入库）

| 步骤 | LightRAG 行为 | 结果 |
|------|--------------|------|
| 3张照片入库（同一UUID未命名） | 自动合并为1个 `person-uuid-999` 实体 | ✅ 无重复 |
| 用户命名"赵磊" | 产生"赵磊"实体，与 `person-uuid-999` 有边连通 | ✅ 可追溯 |
| 后续用"赵磊"入库 | 同名实体自动合并，查询能返回3张照片 | ✅ 完整 |

**核心结论**: 更名不需要特殊处理——LLM 自动建立 UUID 实体和真名实体之间的关联边。

测试脚本: `scripts/test_kg_auto_ingest.py`

---

## 1. 问题根因

三条路径都**手动构建**实体/关系，绕过 LightRAG 的自动提取/合并/建边：

| 路径 | 入口 | 调用方式 | 产生的实体名 |
|------|------|----------|-------------|
| 照片入库 | `_sync_photo_to_kg` | `ainsert_custom_kg` | `person:{uuid}` |
| 梦境整理 | `dream_writer.write_semantic_entity` | `ainsert_custom_kg` | 直接用真名 |
| 聊天提取 | `lightrag_insert` | `ainsert` | LLM决定，通常用真名 |

**结果**: 同一个人产生多个不连通实体。

## 2. 核心思路

**一条指令入库，剩下全交给 LightRAG。**

格式化为结构化文本 → `ainsert` → LightRAG 自动提取/合并/建边。

## 3. 实施步骤

### Step 1: 改造 photo-server `_sync_photo_to_kg`

**文件**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py`

**改动**:
- 删除手动构建 entities/relationships/chunks 的代码（约 130 行）
- 改为格式化照片信息为结构化文本，调用 `lightrag_insert`（走 ainsert 路径）

**旧代码** (行 430-575):
```python
def sync_photo_to_kg(file_path, abstract, detected_persons):
    entities = []
    relationships = []
    # 手动构建 photo entity, person entity, depicts 关系, co_appears_with 关系
    insert_custom_kg(entities=entities, relationships=relationships, ...)
```

**新代码**:
```python
def sync_photo_to_kg(file_path, abstract, detected_persons):
    # 格式化为结构化文本
    text = format_photo_ingest_text(file_path, abstract, detected_persons)
    # 调用 ainsert
    result = lightrag_insert(content=text)
```

**格式化函数**:
```python
def format_photo_ingest_text(file_path, abstract, detected_persons):
    title = Path(file_path).stem
    parts = []
    parts.append(f"照片文件 {title}（照片ID: {file_path}）")

    if abstract:
        # 过滤掉"未命名人物"名称，保留时间和地点信息
        abs_parts = abstract.split("，")
        filtered = [p for p in abs_parts if not p.startswith("未命名人物")]
        if filtered:
            parts.append("，".join(filtered))

    if detected_persons:
        person_descs = []
        for p in detected_persons:
            pid = p.get("id", "")
            pname = p.get("name", "")
            if pname.startswith("未命名人物"):
                person_descs.append(f"一位未命名人物（人物ID: {pid}）")
            else:
                person_descs.append(f"{pname}（人物ID: {pid}）")
        parts.append(f"照片中出现的人物: {'、'.join(person_descs)}")

    return "，".join(parts) + "。"
```

### Step 2: 改造 `name_person` KG 部分

**文件**: `mcp-servers/photo-server/src/niu_photo_server/__init__.py` (行 1896-1952)

**改动**:
- 删除 `lightrag_insert_entity` + `lightrag_list_entities` + `lightrag_merge_entities` + `lightrag_delete_entity` 的复杂清理逻辑（约 50 行）
- 改为一条 `ainsert` 文本，让 LightRAG 自动建立关联

**旧代码**:
```python
# 1. insert_entity(name=f"person:{person_id}", description=name, skip_llm_extraction=True)
# 2. list_fn → 找所有"未命名人物"实体 → merge_fn 合并到 person:{uuid}
# 3. merge_fn(source_entities=[auto_label], target_entity=target_entity)
```

**新代码**:
```python
# 一条 ainsert 命名文本
text = f"人物ID为 {person_id} 的人，用户确认其姓名为{name}。{name}是用户的{关系描述}。"
result = lightrag_insert(content=text)
```

**不再需要**: `lightrag_insert_entity`, `lightrag_list_entities`, `lightrag_merge_entities`, `lightrag_delete_entity` 的调用。LLM 自动处理关联。

### Step 3: 改造 dream_writer

**文件**: `agent/injector/dream_writer.py`

**改动**:
- `write_semantic_entity`: 删除手动构建 entity + anchor relation + chunk，改为格式化文本 + `ainsert`
- `write_episodic_event`: 同上
- `write_semantic_relation`: 同上

**旧代码** (`write_semantic_entity`, 行 91-161):
```python
result = self._ingester.inject_custom_kg(
    entities=[{"entity_name": name, "entity_type": entity_type, "description": description}],
    relationships=[{"src_id": NIU_ENTITY, "tgt_id": name, "keywords": niu_relation, ...}],
    chunks=[{"content": description, ...}],
)
```

**新代码**:
```python
def write_semantic_entity(self, name, entity_type, description):
    niu_relation = self._determine_niu_relation(entity_type)
    text = f"语义记忆: {name}（类型: {entity_type}），{description}。brain:Niu {niu_relation} {name}。"
    result = self._ingester.lightrag_insert(content=text)
```

**旧代码** (`write_episodic_event`, 行 212-346):
```python
result = self._ingester.inject_custom_kg(
    entities=[{"entity_name": full_event_name, "entity_type": EPISODIC_ENTITY_TYPE, ...}],
    relationships=[brain:Niu anchor, time chain, involves],
    chunks=[{"content": description, ...}],
)
```

**新代码**:
```python
def write_episodic_event(self, event_name, description, experience_type, ...):
    text_parts = [f"情景记忆: {event_name}（类型: {experience_type}），{description}。"]
    text_parts.append(f"brain:Niu experienced {EVENT_PREFIX}{event_name}。")
    if prev_event_name:
        chain = "corrected_by" if is_correction else "followed_by"
        text_parts.append(f"{EVENT_PREFIX}{prev_event_name} {chain} {EVENT_PREFIX}{event_name}。")
    if related_entities:
        text_parts.append(f"{EVENT_PREFIX}{event_name} involves {'、'.join(related_entities)}。")
    text = " ".join(text_parts)
    result = self._ingester.lightrag_insert(content=text)
```

### Step 4: 改造 LightRAGIngester 添加 `lightrag_insert` 方法

**文件**: `niu_api/internal/lightrag_adapter.py`

**需要**: `LightRAGIngester` 类添加一个 `lightrag_insert` 方法，直接调用 `rag.ainsert`。

**现有方法**:
- `inject_document` (行 ~1340) — 已有，调用 `rag.ainsert`，但需要 file_paths 参数
- `inject_custom_kg` (行 ~1219) — 调用 `rag.ainsert_custom_kg`

**新增方法**:
```python
def lightrag_insert(self, content: str, file_paths: str = None) -> dict:
    """通过 ainsert 入库结构化文本（LightRAG 自动提取实体/关系）。"""
    rag = get_lightrag()
    kwargs = {}
    if file_paths:
        kwargs["file_paths"] = file_paths
    track_id = call_async(rag.ainsert(content, **kwargs), timeout=600)
    # 等待 pipeline 完成（fire_and_forget 后台处理）
    return {"status": "ok", "track_id": track_id}
```

### Step 5: 清理与标记废弃

1. **`inject_custom_kg`**: 保留但标注 `# DEPRECATED: 建议使用 lightrag_insert`
2. **`lightrag_insert_custom_kg` MCP 工具**: 保留但标注废弃
3. **`lightrag_insert_entity` MCP 工具**: 保留但标注废弃
4. **`lightrag_merge_entities` MCP 工具**: 保留但标注废弃（ainsert 自动处理合并）
5. **region_sync**: 暂不改，脑区节点结构特殊（`brain:region:*`），不适合 ainsert

### Step 6: 向后兼容

**已有图谱数据**: 生产图谱中已有手动构建的 `person:{uuid}` 实体。不需要迁移——新数据走 ainsert 路径后，LLM 会自动建立关联边。

---

## 4. 结构化文本模板

### 照片入库

```
照片文件 {filename}（照片ID: {file_path}），{拍摄日期}，{拍摄地点}。
照片中出现的人物: {真名（人物ID: uuid）} 或 {一位未命名人物（人物ID: uuid）}。
{额外描述}
```

### 人物命名

```
人物ID为 {uuid} 的人，用户确认其姓名为 {real_name}。
{额外信息}
```

### 语义记忆

```
语义记忆: {name}（类型: {entity_type}），{description}。
brain:Niu {relation} {name}。
```

### 情景记忆

```
情景记忆: {event_name}（类型: {experience_type}），{description}。
brain:Niu experienced brain:event:{event_name}。
{时间链描述}
{涉及实体描述}
```

---

## 5. 风险与注意事项

1. **LLM 提取不确定性**: 需设计好文本格式降低错误率。实测表明 LLM 对结构化文本提取效果良好。

2. **性能**: ainsert 比 ainsert_custom_kg 慢（需 LLM 调用），但照片入库不是高频操作，正确性更重要。

3. **UUID 查询**: UUID 不再是实体名，查询通过描述中的 ID 匹配。实测表明 aquery 能通过 UUID 找到关联内容。

4. **LightRAG 删除限制**: `adelete_by_doc_id` 不删除孤立实体。长期运行可能积累无用节点，需定期清理。

5. **region_sync**: 脑区节点（`brain:region:*`）结构特殊，暂不改用 ainsert。

6. **重复文档检测**: LightRAG 通过内容 MD5 检测重复。相同文本不会重复入库。照片入库文本每次不同（不同 file_path），不会触发重复检测。