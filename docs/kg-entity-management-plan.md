# 知识图谱实体管理方案

## TDD 测试结果

| 测试 | 结果 | 含义 |
|------|------|------|
| P0-1 | ⚠️ OVERWRITE | `inject_entity` 对同名实体的 description 是**覆盖**而非合并 |
| P0-2 | ✅ PASS | entity_type 最终为 "person"（小写），前端 `.toLowerCase()` 兼容 |
| P0-3 | ✅ PASS（双向） | depicts 关系双向存储，方向不影响查询 |
| P0-4 | ✅ PASS | 前端分类完全兼容 |
| P0-5 | ⚠️ OVERWRITE | 再次确认 description 覆盖模式 |
| P0-6 | ⚠️ UNKNOWN | source_id 被映射为 UNKNOWN（无 chunk 注册时） |

## 核心发现

### 1. description 覆盖模式

`ainsert_custom_kg` 对同名实体的处理：
- **entity_name 相同** → 调用 `_merge_nodes_then_upsert` 合并
- **description** → 新旧 description 用 `GRAPH_FIELD_SEP`（默认 `\n`）连接合并
- **但 `inject_entity` 只传一个 entity** → 只有一个 description，所以看起来像覆盖

实际行为：如果同时传入多个同名 entity（不同 description），会合并。但 `inject_entity` 每次只传一个，所以新 description **替换**旧的。

**影响**：`name_person` 调用 `inject_entity(description="Renamed to: 张三")` 会覆盖原始的 "张三, detected in photo: xxx"。

### 2. entity_type 大小写

- `sync_photo_to_kg` 用 `"Person"`（大写）
- `name_person`/`merge_persons` 用 `"person"`（小写）
- LightRAG `_merge_nodes_then_upsert` 用 Counter 取最高频次
- 最终 entity_type 为 `"person"`（小写）
- 前端 `mapNodeType()` 做 `.toLowerCase()`，完全兼容

**结论**：大小写不一致不影响功能，但建议统一为 `"Person"`（大写），与 LightRAG 的 `ainsert` 路径（LLM 提取的实体通常大写）保持一致。

### 3. depicts 关系方向

LightRAG 对关系端点做了 `sorted()` 规范化，并在 NetworkX 中存双向边。所以 `photo → person` 和 `person → photo` 都存在，查询不受影响。

### 4. source_id UNKNOWN

`inject_entity` 传入的 `source_id` 在 `ainsert_custom_kg` 中通过 `chunk_to_source_map` 映射。如果没有对应的 chunk 注册，source_id 会被设为 "UNKNOWN"。

**影响**：不影响实体查询，但影响 source_id 过滤功能。

## 修复方案

### Fix 1: name_person — 保留完整 description

**问题**：`name_person` 的 `inject_entity(description="Renamed to: 张三")` 覆盖了原始 description。

**方案**：`name_person` 应该：
1. 先从 KG 读取当前 `person:{uuid}` 实体的 description
2. 构造新的完整 description：`"{name}, {原始description中的照片信息}"`
3. 调用 `inject_entity` 写入完整 description

**代码修改**（`niu_photo_server/__init__.py` line 1854-1863）：

```python
# 同步更新 LightRAG 知识图谱中的实体
try:
    from niu_api.internal.lightrag_adapter import LightRAGIngester
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    ingester = LightRAGIngester()
    rag = get_lightrag()

    # 读取当前实体的 description
    current_desc = ""
    if rag is not None:
        node = call_async(
            rag.chunk_entity_relation_graph.get_node(f"person:{person_id}")
        )
        if node:
            current_desc = node.get("description", "")

    # 构造新 description：名字 + 原始照片信息
    # 原始格式: "任飞, detected in photo: DSC_3272"
    # 改名后: "任飞, detected in photo: DSC_3272"（保持不变，名字已更新）
    new_desc = f"{name}"
    if current_desc:
        # 保留 ", detected in photo: xxx" 部分
        photo_info = current_desc.split(",", 1)
        if len(photo_info) > 1 and "detected in photo" in photo_info[1]:
            new_desc = f"{name},{photo_info[1]}"
        elif "Merged with" in current_desc:
            # 保留合并信息
            new_desc = f"{name}, {current_desc}"

    ingester.inject_entity(
        name=f"person:{person_id}",
        entity_type="Person",  # 统一大写
        description=new_desc,
    )
except Exception as e:
    logger.warning(f"[NAME_PERSON] LightRAG sync failed: {e}")
```

### Fix 2: merge_persons — 删除 person_b 实体 + 统一 entity_type

**问题**：
1. `merge_persons` 只建 `merged_into` 关系，不删除 person_b 实体
2. entity_type 用小写 "person"

**方案**：
1. 合并后删除 `person:{person_b_id}` 实体
2. 更新 `person:{person_a_id}` 的 description 包含合并信息
3. entity_type 统一为 "Person"

**代码修改**（`niu_photo_server/__init__.py` line 2025-2043）：

```python
try:
    from niu_api.internal.lightrag_adapter import LightRAGIngester
    from niu_api.internal.lightrag_manager import get_lightrag, call_async

    ingester = LightRAGIngester()
    merged_name = name_a if name_a else auto_label_a

    # 更新 person_a 实体
    ingester.inject_entity(
        name=f"person:{person_a_id}",
        entity_type="Person",  # 统一大写
        description=f"{merged_name}, merged from {person_b_id}",
    )

    # person_b 合并到 person_a，建立关系
    ingester.inject_relation(
        src_id=f"person:{person_b_id}",
        tgt_id=f"person:{person_a_id}",
        relation="merged_into",
        description=f"Person {person_b_id} merged into {person_a_id}",
    )

    # 删除 person_b 实体（SQLite 已删除，KG 也应删除）
    rag = get_lightrag()
    if rag is not None:
        call_async(
            rag.chunk_entity_relation_graph.delete_node(f"person:{person_b_id}")
        )
except Exception as e:
    logger.warning(f"[MERGE_PERSONS] LightRAG sync failed: {e}")
```

### Fix 3: sync_photo_to_kg — entity_type 统一为 "Person"

当前代码已经用 `"Person"`（大写），无需修改。但 `name_person` 和 `merge_persons` 需要统一。

### Fix 4: ingest_document — 自动调用 LightRAG

**问题**：`ingest_document` 只复制文件返回 `need_l1`，不自动调用 LightRAG。

**方案**：`ingest_document` 在复制文件后，直接调用 `lightrag_insert` 将文档内容写入 KG，返回 `success` 状态。

**代码修改**（`niu_photo_server/__init__.py` line 2540-2549）：

```python
# 自动调用 LightRAG 入库
lightrag_status = "未写入"
if file_content:
    try:
        from niu_api.internal.lightrag_adapter import LightRAGIngester
        ingester = LightRAGIngester()
        result = ingester.inject_document(
            content=file_content,
            doc_id=str(Path(final_path).resolve()),
            file_path=str(Path(final_path).resolve()),
        )
        lightrag_status = "已写入" if result.get("status") == "ok" else "写入失败"
    except Exception as e:
        logger.warning(f"[INGEST] LightRAG auto-insert failed: {e}")
        lightrag_status = f"写入失败: {e}"

return {
    "status": "success",
    "action": action,
    "file_path": str(Path(final_path).resolve()),
    "original_path": str(source),
    "category": category,
    "lightrag": lightrag_status,
}
```

### Fix 5: 文档入库时人名替换

**问题**：文档中写 "张三"，LLM 提取实体名为 "张三"，与照片的 `person:{uuid}` 不自动合并。

**方案**：在文档内容写入 LightRAG 前，替换已知人名为 `person:{uuid}(名字)` 格式。

```python
def replace_person_names(content: str) -> tuple[str, dict[str, str]]:
    """替换文档内容中的已知人名为 person:{uuid}(名字) 格式。

    Returns:
        (替换后内容, 替换映射)
    """
    conn = get_connection()
    cursor = conn.execute(
        "SELECT id, name FROM persons WHERE name IS NOT NULL AND name != '未命名'"
    )
    persons = cursor.fetchall()

    replaced = {}
    for person_id, name in persons:
        if name and len(name) >= 2:  # 至少2个字符才替换
            uuid_format = f"person:{person_id}({name})"
            if name in content:
                content = content.replace(name, uuid_format)
                replaced[name] = uuid_format

    return content, replaced
```

**注意**：这个替换是否有效取决于 LLM 是否会将 `person:{uuid}(张三)` 提取为实体名 `person:{uuid}`。这需要 P1 集成测试验证（需要 LLM proxy 运行）。

## file-processor.md 重写

基于以上修复，file-processor.md 的流程简化为：

### 照片入库
```
photo-server/ingest(path="E:/照片/2024旅行", mode="copy")
```
ingest 内部自动完成：人脸检测 → 人物识别 → KG 同步（person:{uuid} 实体 + depicts 关系 + 同框关系）。

### 文档入库
```
photo-server/ingest(path="E:/tmp/report.pdf", mode="copy")
```
ingest 内部自动完成：文件复制 → 读取内容 → 人名替换 → LightRAG 入库。

### 人物管理
- `name_person` — 改名后自动同步 KG（保留完整 description）
- `merge_persons` — 合并后自动同步 KG（删除 person_b 实体）
- `search_persons` / `get_unnamed_persons` — 查询

## 待验证（P1 集成测试）

1. 文档入库时人名替换后，LLM 是否提取出 `person:{uuid}` 格式的实体名
2. 如果 LLM 不提取 `person:{uuid}` 格式，需要考虑备选方案：
   - 方案 A：文档入库后，搜索 KG 中同名实体，手动调用 `lightrag_merge_entities` 合并
   - 方案 B：文档入库后，用 `inject_entity` 直接注入 `person:{uuid}` 实体（绕过 LLM）
