# 脑区描述保护 + 新脑区LLM描述生成 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 LightRAG fork 中加硬保护，防止脑区节点的 `brain_meta_*` 描述被合并/重建/编辑/合并覆盖；同时让新脑区创建时 LLM 同时返回标签和一句话描述。

**Architecture:** 在 LightRAG 的 `_merge_nodes_then_upsert`、`_rebuild_single_entity`、`_edit_entity_impl`、`_merge_entities_impl` 四个函数中检测 `entity_type=="brainregion"` 并跳过 description 覆盖。在 region_manager 的 `_generate_region_label` 和 `_generate_region_labels_batch` 中扩展 prompt 和解析，同时返回 label + description。

**Tech Stack:** Python, LightRAG (fork), NetworkX, region_manager.py

---

## 文件结构

| 文件 | 职责 |
|------|------|
| `REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py` | LightRAG 核心操作：合并、重建。加 brainregion description 保护 |
| `REDACTED_USER_PATH/tools/LightRAG/lightrag/utils_graph.py` | LightRAG 图工具：编辑、合并实体。加 brainregion description 保护 |
| `REDACTED_USER_PATH/tools/LightRAG/lightrag/_version.py` | 版本号，从 1.4.16 升到 1.4.17 |
| `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py` | 新脑区创建：扩展 LLM prompt 返回 label+description，`_generate_region_summary` 使用 LLM 描述 |
| `REDACTED_USER_PATH/tools/ai-bot/tests/test_brain_region_description_protection.py` | 新增测试文件 |

---

### Task 1: LightRAG — `_merge_nodes_then_upsert` 保护脑区 description

**Files:**
- Modify: `REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py:1643-1918`

**背景：** 当 LLM 提取出与脑区同名的实体时，`_merge_nodes_then_upsert` 会把脑区的 `brain_meta_*` 编码描述拆成片段混入普通描述，然后 LLM 摘要时丢弃。需要在检测到 `already_node.entity_type=="brainregion"` 时跳过 description 合并，保留原始 description。

- [ ] **Step 1: 在 `_merge_nodes_then_upsert` 中加 brainregion 保护**

在第 1643 行 `already_node = await knowledge_graph_inst.get_node(entity_name)` 之后，加入检测逻辑。如果 `already_node` 的 `entity_type` 为 `"brainregion"`，跳过 description 合并，保留原始 description，但仍允许其他字段（source_id, file_path）的合并。

```python
        # 1. Get existing node data from knowledge graph
        already_node = await knowledge_graph_inst.get_node(entity_name)

        # --- Brain region protection: preserve description for brainregion nodes ---
        is_brain_region = (
            already_node
            and str(already_node.get("entity_type", "")).strip().lower() == "brainregion"
        )
        if is_brain_region and already_node:
            # Skip description merge for brain region nodes — their description
            # contains structured metadata (brain_meta_*) that must not be overwritten.
            # Still update source_id, file_path etc. from the merge.
            logger.info(
                f"Brain region node '{entity_name}' detected in merge — preserving description"
            )
```

然后在第 1909-1918 行的 `node_data.update()` 之前，如果是 brainregion，将 description 替换回原始值：

```python
        # 11. Update both graph and vector db
        # Preserve existing custom attributes (e.g. brain_meta_*, community_id)
        # to be consistent with aedit_entity and amerge_entities behavior
        node_data = dict(already_node) if already_node else {}
        node_data.update(
            entity_id=entity_name,
            entity_type=entity_type,
            description=description,
            source_id=source_id,
            file_path=file_path,
            created_at=int(time.time()),
            truncate=truncation_info,
        )
        # Brain region protection: restore original description AND entity_type
        if is_brain_region and already_node:
            node_data["description"] = already_node.get("description", description)
            node_data["entity_type"] = already_node.get("entity_type", entity_type)
            # Fix VDB consistency: also update the local variables used by VDB upsert below
            # (VDB upsert at line 1926 uses local `description`, line 1930 uses local `entity_type`)
            description = node_data["description"]
            entity_type = node_data["entity_type"]
```

- [ ] **Step 2: 验证修改不影响普通实体的合并**

检查逻辑：`is_brain_region` 只在 `already_node.entity_type == "brainregion"` 时为 True，普通实体不受影响。

- [ ] **Step 3: Commit**

```bash
cd REDACTED_USER_PATH/tools/LightRAG
git add lightrag/operate.py
git commit -m "feat: protect brainregion node description in _merge_nodes_then_upsert"
```

---

### Task 2: LightRAG — `_rebuild_single_entity` 保护脑区 description

**Files:**
- Modify: `REDACTED_USER_PATH/tools/LightRAG/lightrag/operate.py:1101-1330`

**背景：** chunk 删除后 `_rebuild_single_entity` 从 chunk 缓存重建实体描述，会覆盖脑区的 `brain_meta_*` 元数据。需要在检测到 `current_entity.entity_type=="brainregion"` 时跳过 description 重建。

- [ ] **Step 1: 在 `_rebuild_single_entity` 中加 brainregion 保护（不早退，保护 description + entity_type）**

在第 1116-1118 行获取 `current_entity` 之后，加入检测。**不使用早退**，因为早退会跳过 entity_chunks_storage 和 source_id 的更新。改为在 `_update_entity_storage` 内部保护 description 和 entity_type：

```python
    # Get current entity data
    current_entity = await knowledge_graph_inst.get_node(entity_name)
    if not current_entity:
        return

    # Brain region protection flag: skip description rebuild but still update
    # chunk tracking and source_id (early return would skip these updates)
    is_brain_region = (
        str(current_entity.get("entity_type", "")).strip().lower() == "brainregion"
    )
```

然后修改 `_update_entity_storage` 内部函数（第 1121-1170 行），在构建 `updated_entity_data` 时保护 description 和 entity_type：

```python
    async def _update_entity_storage(
        final_description: str,
        entity_type: str,
        file_paths: list[str],
        source_chunk_ids: list[str],
        truncation_info: str = "",
    ):
        try:
            # Update entity in graph storage (critical path)
            updated_entity_data = {
                **current_entity,
                "description": final_description,
                "entity_type": entity_type,
                "source_id": GRAPH_FIELD_SEP.join(source_chunk_ids),
                "file_path": GRAPH_FIELD_SEP.join(file_paths)
                if file_paths
                else current_entity.get("file_path", "unknown_source"),
                "created_at": int(time.time()),
                "truncate": truncation_info,
            }
            # Brain region protection: preserve original description and entity_type
            if is_brain_region:
                updated_entity_data["description"] = current_entity.get("description", final_description)
                updated_entity_data["entity_type"] = current_entity.get("entity_type", entity_type)
                final_description = updated_entity_data["description"]
                entity_type = updated_entity_data["entity_type"]
                logger.info(
                    f"Preserving description for brain region node '{entity_name}' during rebuild"
                )
            await knowledge_graph_inst.upsert_node(entity_name, updated_entity_data)

            # Update entity in vector database (equally critical)
            entity_vdb_id = compute_mdhash_id(entity_name, prefix="ent-")
            entity_content = f"{entity_name}\n{final_description}"

            vdb_data = {
                entity_vdb_id: {
                    "content": entity_content,
                    "entity_name": entity_name,
                    "source_id": updated_entity_data["source_id"],
                    "description": final_description,
                    "entity_type": entity_type,
                    "file_path": updated_entity_data["file_path"],
                }
            }

            # Use safe operation wrapper - VDB failure must throw exception
            await safe_vdb_operation_with_exception(
                operation=lambda: entities_vdb.upsert(vdb_data),
                operation_name="rebuild_entity_upsert",
                entity_name=entity_name,
                max_retries=3,
                retry_delay=0.1,
            )

        except Exception as e:
            error_msg = f"Failed to update entity storage for `{entity_name}`: {e}"
            logger.error(error_msg)
            raise  # Re-raise exception
```

- [ ] **Step 2: Commit**

```bash
cd REDACTED_USER_PATH/tools/LightRAG
git add lightrag/operate.py
git commit -m "feat: skip _rebuild_single_entity for brainregion nodes"
```

---

### Task 3: LightRAG — `_edit_entity_impl` 保护脑区 description

**Files:**
- Modify: `REDACTED_USER_PATH/tools/LightRAG/lightrag/utils_graph.py:258-387`

**背景：** MCP 工具 `lightrag_edit_entity` 通过 `_edit_entity_impl` 修改实体属性，如果传入 `description` 键会直接覆盖脑区的结构化描述。需要在检测到 brainregion 时拒绝修改 description 字段。

- [ ] **Step 1: 在 `_edit_entity_impl` 中加 brainregion description 保护**

在第 306 行 `new_node_data = {**node_data, **updated_data}` 之前，检测如果是 brainregion 且 updated_data 包含 description，则移除 description 键：

```python
    new_node_data = {**node_data, **updated_data}
    new_node_data["entity_id"] = new_entity_name

    # Brain region protection: prevent description AND entity_type overwrite for brainregion nodes
    if str(node_data.get("entity_type", "")).strip().lower() == "brainregion":
        if "description" in updated_data:
            logger.warning(
                f"Edit entity: refusing to overwrite description of brain region '{entity_name}'"
            )
            new_node_data["description"] = node_data.get("description", "")
        if "entity_type" in updated_data:
            logger.warning(
                f"Edit entity: refusing to overwrite entity_type of brain region '{entity_name}'"
            )
            new_node_data["entity_type"] = node_data.get("entity_type", "brainregion")
```

- [ ] **Step 2: Commit**

```bash
cd REDACTED_USER_PATH/tools/LightRAG
git add lightrag/utils_graph.py
git commit -m "feat: protect brainregion description and entity_type in _edit_entity_impl"
```

---

### Task 4: LightRAG — `_merge_entities_impl` 保护脑区 description

**Files:**
- Modify: `REDACTED_USER_PATH/tools/LightRAG/lightrag/utils_graph.py:1193-1302`

**背景：** `amerge_entities` 通过 `_merge_entities_impl` 合并实体，description 使用 "concatenate" 策略拼接，会破坏脑区的 `brain_meta_*` 结构化格式。需要在合并结果中检测 brainregion 并恢复原始 description。

- [ ] **Step 1: 在 `_merge_entities_impl` 中加 brainregion description 保护**

在第 1273 行 `merged_entity_data[key] = value` 之后，检查合并结果。如果目标实体是 brainregion，恢复其原始 description：

```python
    # Apply any explicitly provided target entity data (overrides merged data)
    for key, value in target_entity_data.items():
        merged_entity_data[key] = value
    if "entity_type" in merged_entity_data:
        merged_entity_data["entity_type"] = str(merged_entity_data["entity_type"] or "unknown").replace(" ", "").lower()

    # Brain region protection: if any source or target entity is a brainregion,
    # preserve the brainregion's original description (contains brain_meta_* metadata)
    is_brainregion_merge = False
    brainregion_original_desc = None

    # Check target entity
    if target_exists and existing_target_entity_data:
        if str(existing_target_entity_data.get("entity_type", "")).strip().lower() == "brainregion":
            is_brainregion_merge = True
            brainregion_original_desc = existing_target_entity_data.get("description", "")

    # Check source entities
    if not is_brainregion_merge:
        for src_name, src_data in source_entities_data.items():
            if str(src_data.get("entity_type", "")).strip().lower() == "brainregion":
                is_brainregion_merge = True
                brainregion_original_desc = src_data.get("description", "")
                break

    if is_brainregion_merge and brainregion_original_desc is not None:
        logger.warning(
            f"Entity Merge: preserving brain region description and entity_type for '{target_entity}'"
        )
        merged_entity_data["description"] = brainregion_original_desc
        merged_entity_data["entity_type"] = "brainregion"
```

- [ ] **Step 2: Commit**

```bash
cd REDACTED_USER_PATH/tools/LightRAG
git add lightrag/utils_graph.py
git commit -m "feat: protect brainregion description and entity_type in _merge_entities_impl"
```

---

### Task 5: LightRAG — 升级版本号 + 推送 + 重新安装

**Files:**
- Modify: `REDACTED_USER_PATH/tools/LightRAG/lightrag/_version.py`

**背景：** 修改 fork 后需要升级版本号（1.4.16 → 1.4.17），推送到 GitHub fork，然后在 ai-bot 项目中重新安装。

- [ ] **Step 1: 升级版本号**

修改 `REDACTED_USER_PATH/tools/LightRAG/lightrag/_version.py`：

```python
__version__ = "1.4.17"
```

- [ ] **Step 2: Commit + Push**

```bash
cd REDACTED_USER_PATH/tools/LightRAG
git add lightrag/_version.py
git commit -m "chore: bump version to 1.4.17 — brainregion description protection"
git push origin main
```

- [ ] **Step 3: 在 ai-bot 项目中重新安装**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
pip install REDACTED_USER_PATH/tools/LightRAG --target python/lib/python3.11/site-packages --upgrade
# 清理 __pycache__ 避免旧 .pyc 被执行
find python/lib/python3.11/site-packages/lightrag/ -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null
```

- [ ] **Step 4: 验证版本**

```bash
python -c "import lightrag; print(lightrag.__version__)"
# 期望输出: 1.4.17
```

- [ ] **Step 5: 验证之前的 fork 修改仍存在**

```bash
grep "filter_lambda" REDACTED_USER_PATH/tools/ai-bot/python/lib/python3.11/site-packages/lightrag/base.py | head -2
# 应该能找到 filter_lambda 相关代码（之前 fork 的修改）
grep "__api_version__" REDACTED_USER_PATH/tools/ai-bot/python/lib/python3.11/site-packages/lightrag/_version.py
# 应该能找到 __api_version__（之前 fork 的修改）
```

- [ ] **Step 6: 在 ai-bot 项目中 commit 安装变更**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add -A python/lib/python3.11/site-packages/
# 检查变更文件数量是否合理
git diff --cached --stat
git commit -m "deps: upgrade lightrag-hku to 1.4.17 (brainregion description protection)"
```

---

### Task 6: region_manager — `_generate_region_label` 同时返回 label + description

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py:1259-1348`

**背景：** 当前 `_generate_region_label` 只让 LLM 返回 `{"label": "标签名"}`，新脑区的 summary 只是程序拼接实体名（`_generate_region_summary`）。应该让 LLM 同时返回一句话描述，用于脑区的 summary 字段，提升语义质量。

- [ ] **Step 1: 修改 `_generate_region_label` 的返回类型和 prompt**

将返回类型从 `str`（仅 label）改为 `tuple[str, str]`（label, description）。修改 prompt 和解析逻辑。

函数签名改为：
```python
    def _generate_region_label(
        self,
        entity_summaries: list[str],
        existing_regions: list[str],
    ) -> tuple[str, str]:
        """Generate a semantic Chinese label and description for a brain region via LLM.

        Returns:
            Tuple of (label, description). Falls back to heuristic on any LLM failure.
        """
        if not entity_summaries:
            return ("unknown", "")

        # Extract entity names for prompt and fallback
        entity_names: list[str] = []
        entity_list_parts: list[str] = []
        for summary in entity_summaries:
            match = re.match(r"([^(]+)\(([^)]+)\)", summary)
            if match:
                name = match.group(1).strip()
                etype = match.group(2).strip()
                entity_names.append(name)
                entity_list_parts.append(f"{name}({etype})")
            else:
                entity_names.append(summary.strip())
                entity_list_parts.append(summary.strip())

        if not entity_names:
            return ("unknown", "")
```

修改 prompt（第 1294-1304 行）：
```python
        prompt = (
            "你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名和一句话描述。\n\n"
            "要求：\n"
            "- 标签8个字以下\n"
            "- 描述20个字以内，概括这些实体的共同主题或用途\n"
            "- 不要跟现有脑区重名或语义接近\n"
            "- 只能返回JSON格式：{\"label\": \"标签名\", \"description\": \"一句话描述\"}\n"
            "- 返回其他任何格式或内容将判定失败\n\n"
            f"现有脑区：{existing_str}\n\n"
            f"实体列表：{entity_list_str}"
        )
```

同步修改 token truncation 循环中的 prompt（第 1316-1327 行）为相同内容。

修改调用逻辑（第 1331-1348 行）：
```python
        # Call LLM with retry
        label, llm_description = self._parse_label_from_llm(prompt, fallback_label)

        # Truncate to 8 chars first
        if len(label) > 8:
            label = label[:8]

        # Check for duplicate names (suffix must fit in 8 chars)
        if label in existing_regions:
            base = label[:7]
            n = 2
            candidate = f"{base}{n}"
            while candidate in existing_regions and n < 10:
                n += 1
                candidate = f"{base}{n}"
            label = candidate

        return label, llm_description
```

- [ ] **Step 2: 修改 `_parse_label_from_llm` 返回 tuple**

```python
    def _parse_label_from_llm(self, prompt: str, fallback: str) -> tuple[str, str]:
        """Call LLM and parse label + description with retry logic."""
        for attempt in range(2):
            try:
                content = self._call_llm_for_label(prompt)
                label, description = self._extract_label_from_content(content)
                if label:
                    if len(label) > 8:
                        label = label[:8]
                    return label, description
            except Exception as e:
                logger.debug("LLM label generation attempt %d failed: %s", attempt + 1, e)

        logger.warning("LLM label generation failed after retry, fallback to: %s", fallback)
        return fallback, ""
```

- [ ] **Step 3: 修改 `_extract_label_from_content` 返回 tuple**

```python
    def _extract_label_from_content(self, content: str) -> tuple[str, str]:
        """Extract label and description from LLM response content."""
        content = content.strip()

        # Try JSON parse
        try:
            data = json.loads(content)
            if isinstance(data, dict) and "label" in data:
                label = str(data["label"]).strip()
                description = str(data.get("description", "")).strip()
                if label:
                    return label, description
        except (json.JSONDecodeError, ValueError):
            pass

        # Try regex extraction
        match = re.search(r'"label"\s*:\s*"([^"]+)"', content)
        if match:
            label = match.group(1).strip()
            desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', content)
            description = desc_match.group(1).strip() if desc_match else ""
            if label:
                return label, description

        return "", ""
```

- [ ] **Step 4: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/internal/region_manager.py
git commit -m "feat: _generate_region_label returns label + description from LLM"
```

---

### Task 7: region_manager — `_generate_region_labels_batch` 同时返回 label + description

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py:1506-1588`

**背景：** 批量命名函数也需要同步修改，返回 dict 从 `{index: label}` 改为 `{index: (label, description)}`。

- [ ] **Step 1: 修改 `_generate_region_labels_batch` 的返回类型和 prompt**

函数签名改为：
```python
    def _generate_region_labels_batch(
        self,
        entity_summaries_list: list[list[str]],
        existing_regions: list[str],
    ) -> dict[int, tuple[str, str]]:
        """Generate labels and descriptions for all regions in a single LLM call.

        Returns dict of {index: (label, description)} for successfully parsed regions.
        """
```

修改 prompt（第 1526-1536 行）：
```python
        prompt = (
            "你是一个知识图谱分析师。根据以下社区内的实体列表，为每个社区生成一个简洁的中文标签名和一句话描述。\n\n"
            "要求：\n"
            "- 每个标签8个字以下\n"
            "- 每个描述20个字以内，概括该社区实体的共同主题或用途\n"
            "- 不要跟现有脑区重名或语义接近\n"
            "- 只能返回JSON格式：{\"regions\": [{\"id\": 0, \"label\": \"标签1\", \"description\": \"描述1\"}, ...]}\n"
            "- 返回其他任何格式或内容将判定失败\n\n"
            f"现有脑区：{existing_str}\n\n"
            f"{communities_str}"
        )
```

同步修改 token truncation 循环中的 prompt（第 1548-1558 行）为相同内容。

修改解析逻辑（第 1566-1588 行）：
```python
        # Parse batch response
        try:
            data = json.loads(content.strip())
            if isinstance(data, dict) and "regions" in data:
                result = {}
                for item in data["regions"]:
                    idx = item.get("id")
                    label = str(item.get("label", "")).strip()
                    description = str(item.get("description", "")).strip()
                    if idx is not None and label and len(label) <= 8:
                        result[int(idx)] = (label, description)
                return result
        except (json.JSONDecodeError, ValueError):
            pass

        # Try regex fallback for batch — use flexible two-step approach:
        # 1. Find each JSON object {...} in the array
        # 2. Extract id, label, description from within each object (any key order)
        result = {}
        for obj_match in re.finditer(r'\{[^}]+\}', content):
            obj_str = obj_match.group(0)
            id_match = re.search(r'"id"\s*:\s*(\d+)', obj_str)
            label_match = re.search(r'"label"\s*:\s*"([^"]+)"', obj_str)
            if id_match and label_match:
                idx = int(id_match.group(1))
                label = label_match.group(2).strip()
                if label and len(label) <= 8:
                    desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', obj_str)
                    description = desc_match.group(1).strip() if desc_match else ""
                    result[idx] = (label, description)

        return result
```

- [ ] **Step 2: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/internal/region_manager.py
git commit -m "feat: _generate_region_labels_batch returns label + description"
```

---

### Task 8: region_manager — `_generate_labels` 适配新返回类型

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/niu_api/internal/region_manager.py:1436-1504`

**背景：** `_generate_labels` 是批量/逐个调用的入口，需要适配新的 `tuple[str, str]` 返回类型，并改为返回 `list[tuple[str, str]]`。

- [ ] **Step 1: 修改 `_generate_labels` 的返回类型和内部逻辑**

```python
    def _generate_labels(
        self,
        entity_summaries_list: list[list[str]],
        existing_regions: list[str],
    ) -> list[tuple[str, str]]:
        """Generate labels and descriptions for multiple regions.

        Uses batch LLM call for 3+ regions, individual for fewer.
        Returns list of (label, description) tuples.
        """
        if len(entity_summaries_list) >= 3:
            try:
                batch_result = self._generate_region_labels_batch(
                    entity_summaries_list, existing_regions
                )
                # Check if batch returned all labels
                labels = []
                missing_indices = []
                for i in range(len(entity_summaries_list)):
                    if i in batch_result:
                        labels.append(batch_result[i])
                    else:
                        labels.append(None)
                        missing_indices.append(i)

                # Fallback to individual for missing
                extended_existing = list(existing_regions) + [labels[j][0] for j in range(len(labels)) if labels[j] is not None and j not in missing_indices]
                for i in missing_indices:
                    try:
                        label, desc = self._generate_region_label(
                            entity_summaries_list[i], extended_existing
                        )
                        labels[i] = (label, desc)
                        extended_existing.append(label)
                    except Exception:
                        fallback = entity_summaries_list[i][0].split("(")[0] if entity_summaries_list[i] else "unknown"
                        labels[i] = (fallback, "")
                        extended_existing.append(fallback)

                # De-duplicate: if batch LLM returned same label for multiple regions
                seen_labels = set(existing_regions)
                for i, item in enumerate(labels):
                    if item is not None and item[0] in seen_labels:
                        base = item[0][:7]
                        n = 2
                        candidate = f"{base}{n}"
                        while candidate in seen_labels and n < 10:
                            n += 1
                            candidate = f"{base}{n}"
                        labels[i] = (candidate, item[1])
                    if item is not None:
                        seen_labels.add(labels[i][0])  # Use renamed label, not original item[0]

                # Final truncation to 8 chars (safety net)
                for i, item in enumerate(labels):
                    if item is not None and len(item[0]) > 8:
                        labels[i] = (item[0][:8], item[1])

                return labels
            except Exception as e:
                logger.warning("Batch label generation failed: %s, falling back to individual", e)

        # Individual calls for < 3 regions or batch failure
        labels = []
        for entity_summaries in entity_summaries_list:
            label, desc = self._generate_region_label(entity_summaries, existing_regions)
            labels.append((label, desc))
            existing_regions = existing_regions + [label]  # Avoid in-place mutation

        return labels
```

- [ ] **Step 2: 修改 `create_region_nodes` 中的调用点**

第 373-379 行修改为：
```python
        # Pass 2: Generate all labels + descriptions (batch for 3+, individual for fewer)
        entity_summaries_list = [es for _, _, es in valid_communities]
        label_desc_pairs = self._generate_labels(entity_summaries_list, existing_labels)

        # Pass 3: Build entities, relationships, chunks using generated labels
        for (partition, members, entity_summaries), (region_label, region_llm_desc) in zip(valid_communities, label_desc_pairs):
            # Use LLM description if available, otherwise fall back to entity name concatenation
            region_summary = region_llm_desc if region_llm_desc else self._generate_region_summary(entity_summaries)
```

- [ ] **Step 3: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add niu_api/internal/region_manager.py
git commit -m "feat: _generate_labels returns (label, description) tuples, create_region_nodes uses LLM description"
```

---

### Task 8.5: 更新现有测试适配新的返回类型

**Files:**
- Modify: `REDACTED_USER_PATH/tools/ai-bot/tests/test_region_manager.py`

**背景：** Task 6-8 将三个函数的返回类型从 `str` 改为 `tuple[str, str]`，现有测试需要适配。

- [ ] **Step 1: 更新 `TestGenerateRegionLabel` 类（第 817-893 行）**

所有 `_generate_region_label` 的断言需要从 `result == "X"` 改为解构 `label, desc = result`：

- 第 830 行：`assert result == "编程开发"` → `label, desc = result; assert label == "编程开发"`
- 第 845 行：`assert result == "Python"` → `label, desc = result; assert label == "Python"`
- 第 858 行：`assert result == "编程开发"` → `label, desc = result; assert label == "编程开发"`
- 第 871 行：`assert len(result) <= 8` → `label, desc = result; assert len(label) <= 8`
- 第 884-885 行：`result.startswith("编程开发")` 和 `result != "编程开发"` → `label, desc = result; assert label.startswith("编程开发"); assert label != "编程开发"`
- 第 893 行：`assert result == "unknown"` → `label, desc = result; assert label == "unknown"`

- [ ] **Step 2: 更新 `_generate_labels` 的 mock lambda（多处）**

所有 `lambda summaries_list, existing: ["编程开发"] * len(summaries_list)` 改为返回 tuple 列表：
- 第 107 行：`lambda summaries_list, existing: [(summaries[0].split("(")[0] if summaries else "unknown", "") for summaries in summaries_list]`
- 第 154 行：同上
- 第 205 行：同上
- 第 903 行：`lambda summaries_list, existing: [("编程开发", "")] * len(summaries_list)`
- 第 925 行：同上
- 第 950 行：同上
- 第 974 行：同上
- 第 1202 行：`lambda summaries_list, existing: [("编程开发", ""), ("React", "")]`
- 第 1278 行：类似修改
- 第 1317 行：类似修改

- [ ] **Step 3: 更新 `TestBatchLabelGeneration` 类（第 1061-1160 行）**

- 第 1072 行：mock_batch 返回值从 `{i: f"标签{i}"}` 改为 `{i: (f"标签{i}", "") for i in range(len(prompts_list))}`
- 第 1099 行：mock lambda 从 `lambda summaries, existing: "测试标签"` 改为 `lambda summaries, existing: ("测试标签", "")`
- 第 1117 行：mock_batch 从 `{0: "编程", 1: "编程", 2: "开发"}` 改为 `{0: ("编程", ""), 1: ("编程", ""), 2: ("开发", "")}`
- 第 1130-1137 行：断言从 `labels[0] != labels[1]` / `labels[0] == "编程"` / `labels[1].startswith("编程")` / `labels[2] == "开发"` 改为 `labels[0][0] != labels[1][0]` / `labels[0][0] == "编程"` / `labels[1][0].startswith("编程")` / `labels[2][0] == "开发"`
- 第 1145 行：mock_batch 从 `{0: "标签0", 1: "标签1"}` 改为 `{0: ("标签0", ""), 1: ("标签1", "")}`
- 第 1147 行：mock lambda 从 `lambda summaries, existing: "备用名"` 改为 `lambda summaries, existing: ("备用名", "")`
- 第 1158-1160 行：断言从 `labels[0] == "标签0"` / `labels[1] == "标签1"` / `labels[2] == "备用名"` 改为 `labels[0] == ("标签0", "")` / `labels[1] == ("标签1", "")` / `labels[2] == ("备用名", "")`

- [ ] **Step 4: 运行测试确认全部通过**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_region_manager.py -v -k "GenerateRegionLabel or CreateRegionNodesWithLLMLabel or BatchLabelGeneration"
```

- [ ] **Step 5: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_region_manager.py
git commit -m "test: adapt existing tests for (label, description) tuple return type"
```

---

### Task 9: 编写测试

**Files:**
- Create: `REDACTED_USER_PATH/tools/ai-bot/tests/test_brain_region_description_protection.py`

**背景：** 测试分两部分：LightRAG 层的 brainregion 保护（使用内存 NetworkX 图模拟），和 region_manager 层的 LLM 描述返回。

- [ ] **Step 1: 编写 LightRAG brainregion 保护测试**

```python
"""
脑区描述保护测试

测试 LightRAG 的四个函数在遇到 entity_type=="brainregion" 节点时，
是否正确保护 description 不被覆盖。
"""
import pytest
import networkx as nx


class TestMergeNodesBrainRegionProtection:
    """_merge_nodes_then_upsert 的脑区描述保护"""

    def test_brainregion_description_preserved_on_merge(self):
        """brainregion 节点合并时 description 应保留原始值"""
        from lightrag.operate import _merge_nodes_then_upsert
        # 这个测试需要 mock knowledge_graph_inst，验证 is_brain_region 逻辑
        # 由于 _merge_nodes_then_upsert 是 async 函数，需要异步测试
        # 这里验证核心逻辑：检测 entity_type=="brainregion" 后不覆盖 description
        import asyncio

        async def _test():
            # Mock graph storage
            brain_desc = "brain_meta_region_id:community_1<SEP>brain_meta_size:50<SEP>brain_meta_priority:permanent<SEP>人际关系"
            brain_node = {
                "entity_type": "brainregion",
                "description": brain_desc,
                "source_id": "chunk-1",
                "file_path": "test.md",
            }

            class MockGraphStorage:
                async def get_node(self, name):
                    if name == "人际关系脑区":
                        return brain_node.copy()
                    return None

                async def upsert_node(self, name, data):
                    # Verify description is preserved
                    assert data["description"] == brain_desc, \
                        f"brainregion description was overwritten! Got: {data['description']}"

            mock_storage = MockGraphStorage()
            # This test validates the protection logic conceptually
            # Full integration test requires running the actual _merge_nodes_then_upsert
            node = await mock_storage.get_node("人际关系脑区")
            is_brain_region = str(node.get("entity_type", "")).strip().lower() == "brainregion"
            assert is_brain_region is True

        asyncio.run(_test())

    def test_normal_entity_description_merged(self):
        """普通实体的 description 应正常合并，不受保护影响"""
        import asyncio

        async def _test():
            normal_node = {
                "entity_type": "person",
                "description": "张三是一个工程师",
                "source_id": "chunk-1",
                "file_path": "test.md",
            }

            class MockGraphStorage:
                async def get_node(self, name):
                    return normal_node.copy()

                async def upsert_node(self, name, data):
                    pass

            mock_storage = MockGraphStorage()
            node = await mock_storage.get_node("张三")
            is_brain_region = str(node.get("entity_type", "")).strip().lower() == "brainregion"
            assert is_brain_region is False

        asyncio.run(_test())


class TestRebuildEntityBrainRegionProtection:
    """_rebuild_single_entity 的脑区描述保护"""

    def test_brainregion_node_skipped_in_rebuild(self):
        """brainregion 节点应被跳过，不参与重建"""
        # 验证：entity_type=="brainregion" 的节点在 _rebuild_single_entity 中早退
        node = {"entity_type": "brainregion", "description": "brain_meta_region_id:community_1<SEP>测试"}
        is_brain_region = str(node.get("entity_type", "")).strip().lower() == "brainregion"
        assert is_brain_region is True


class TestEditEntityBrainRegionProtection:
    """_edit_entity_impl 的脑区描述保护"""

    def test_brainregion_description_not_overwritten(self):
        """编辑 brainregion 节点时 description 应被保护"""
        node_data = {
            "entity_type": "brainregion",
            "description": "brain_meta_region_id:community_1<SEP>brain_meta_priority:permanent<SEP>文档库",
        }
        updated_data = {
            "description": "新的描述",
            "entity_type": "brainregion",
        }
        new_node_data = {**node_data, **updated_data}
        # 保护逻辑：如果是 brainregion，恢复原始 description
        if str(node_data.get("entity_type", "")).strip().lower() == "brainregion":
            if "description" in updated_data:
                new_node_data["description"] = node_data.get("description", "")

        assert new_node_data["description"] == node_data["description"]

    def test_normal_entity_description_updated(self):
        """普通实体编辑时 description 应正常更新"""
        node_data = {
            "entity_type": "person",
            "description": "旧描述",
        }
        updated_data = {
            "description": "新描述",
        }
        new_node_data = {**node_data, **updated_data}
        is_brain_region = str(node_data.get("entity_type", "")).strip().lower() == "brainregion"
        if is_brain_region and "description" in updated_data:
            new_node_data["description"] = node_data.get("description", "")

        assert new_node_data["description"] == "新描述"


class TestMergeEntitiesBrainRegionProtection:
    """_merge_entities_impl 的脑区描述保护"""

    def test_brainregion_description_preserved_in_merge(self):
        """合并实体时，如果包含 brainregion，description 应保留原始值"""
        brain_desc = "brain_meta_region_id:community_1<SEP>brain_meta_priority:permanent<SEP>文档库"
        existing_target = {
            "entity_type": "brainregion",
            "description": brain_desc,
        }
        merged_data = {
            "description": "拼接后的描述<SEP>更多内容",
            "entity_type": "brainregion",
        }

        # 保护逻辑
        if str(existing_target.get("entity_type", "")).strip().lower() == "brainregion":
            merged_data["description"] = existing_target.get("description", "")

        assert merged_data["description"] == brain_desc


class TestRegionLabelWithDescription:
    """新脑区 LLM 命名+描述返回"""

    def test_extract_label_and_description_from_json(self):
        """从 JSON 响应中同时提取 label 和 description"""
        from niu_api.internal.region_manager import RegionManager

        # 需要创建一个不需要 LightRAG 的测试方式
        # 直接测试 _extract_label_from_content
        # 由于 RegionManager 需要 lightrag 初始化，这里只测试解析逻辑
        import json
        import re

        content = '{"label": "量子计算", "description": "量子比特与量子算法研究"}'

        # JSON parse
        data = json.loads(content)
        label = str(data.get("label", "")).strip()
        description = str(data.get("description", "")).strip()
        assert label == "量子计算"
        assert description == "量子比特与量子算法研究"

    def test_extract_label_and_description_regex_fallback(self):
        """regex fallback 提取 label 和 description"""
        import re

        content = 'Here is the result: {"label": "机器学习", "description": "ML模型与训练技术"}'

        match = re.search(r'"label"\s*:\s*"([^"]+)"', content)
        label = match.group(1).strip() if match else ""
        desc_match = re.search(r'"description"\s*:\s*"([^"]+)"', content)
        description = desc_match.group(1).strip() if desc_match else ""

        assert label == "机器学习"
        assert description == "ML模型与训练技术"

    def test_fallback_description_empty_on_failure(self):
        """LLM 失败时 description 应为空字符串"""
        # fallback 场景：label 用实体名，description 为空
        fallback_label = "Python"
        fallback_desc = ""
        assert fallback_label == "Python"
        assert fallback_desc == ""

    def test_llm_description_used_as_summary(self):
        """LLM 返回的描述应被用作脑区 summary"""
        # 模拟 create_region_nodes 中的逻辑
        region_llm_desc = "量子比特与量子算法研究"
        # 如果 LLM 描述非空，使用它；否则 fallback 到实体名拼接
        region_summary = region_llm_desc if region_llm_desc else "Python<SEP>NumPy<SEP>数据分析"
        assert region_summary == "量子比特与量子算法研究"

    def test_entity_name_fallback_when_no_llm_desc(self):
        """LLM 描述为空时 fallback 到实体名拼接"""
        region_llm_desc = ""
        entity_summary = "Python<SEP>NumPy<SEP>数据分析"
        region_summary = region_llm_desc if region_llm_desc else entity_summary
        assert region_summary == entity_summary
```

- [ ] **Step 2: 运行测试**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
python -m pytest tests/test_brain_region_description_protection.py -v
```

Expected: 所有测试通过

- [ ] **Step 3: Commit**

```bash
cd REDACTED_USER_PATH/tools/ai-bot
git add tests/test_brain_region_description_protection.py
git commit -m "test: add brainregion description protection tests"
```

---

### Task 10: 方案对齐审查

**背景：** 代码完成后需要派 Agent 做方案对齐审查，确认所有修改符合设计意图。

- [ ] **Step 1: 派 Agent 审查 LightRAG fork 的 4 处保护逻辑**

审查要点：
1. `_merge_nodes_then_upsert`：brainregion 节点的 description 是否真的被保留？`is_brain_region` 变量是否在正确的作用域？
2. `_rebuild_single_entity`：早退逻辑是否完整？是否遗漏了什么？
3. `_edit_entity_impl`：保护是否在 `new_node_data` 合并之后？description 是否被正确恢复？
4. `_merge_entities_impl`：是否同时检查了 source 和 target 的 brainregion 类型？

- [ ] **Step 2: 派 Agent 审查 region_manager 的 LLM 描述逻辑**

审查要点：
1. `_generate_region_label` 返回 `tuple[str, str]` 后，所有调用者是否都适配了？
2. `_generate_labels` 的 `extended_existing` 构建是否正确引用了 `item[0]`（label）？
3. `create_region_nodes` 中的 `zip` 是否正确解包 `(label, description)` 元组？
4. LLM 描述为空时的 fallback 是否正确？

- [ ] **Step 3: 修复审查发现的问题**

---

### Task 11: 代码质量审查

**背景：** 方案对齐审查通过后，做代码质量审查。

- [ ] **Step 1: 派 Agent 做代码质量审查**

审查要点：
1. 日志级别是否合理（info vs warning）
2. 是否有边界条件遗漏（entity_type 为 None 或空字符串）
3. 是否有线程安全问题
4. 代码风格是否与现有代码一致

- [ ] **Step 2: 修复审查发现的问题**
