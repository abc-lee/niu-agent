# 脑区 LLM 命名改进设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将脑区命名从启发式（取第一个实体名）改为 LLM 生成语义化名称，同时修复 description 格式和 chunks 缺失问题，使脑区可被向量检索命中。

**Architecture:** 在 `_summarize_region()` 中调用 LiteLLMSession 同步生成脑区标签名；description 改为 top-10 实体名 `<SEP>` 拼接（利于向量检索）；`inject_custom_kg` 必须带 chunks（使脑区可被语义连接）。

**Tech Stack:** LiteLLMSession（已有 LLM 抽象层）、token 计算函数（SDK 提供）

---

## 现状问题

1. **命名无语义**：`_summarize_region()` 取 `entity_names[0]`（igraph 顶点 ID 最小的成员）作为脑区标签，跟社区主题无关
2. **description 不可检索**：格式为 `"Python(skill)、Django(framework)等3个实体<SEP>brain_meta_..."` ，向量检索无法命中
3. **chunks=[] 导致死实体**：`create_region_nodes()` 调用 `inject_custom_kg(entities=..., relationships=..., chunks=[])` ，脑区在向量空间不可见，大模型无法主动跟它建关系

## 改动范围

### 1. `_summarize_region()` — LLM 生成标签名

**当前**：`region_label = entity_names[0]` （启发式，取第一个实体名）

**改为**：调用 LiteLLMSession 生成 8 字以下中文标签

**输入组装**：
- 社区内实体名+类型列表：`"Python(skill), Django(framework), FastAPI(framework), ..."`
- 现有脑区标签列表（避免重名）：从 `RegionManager.get_all_regions()` 获取
- 组装完后用 SDK token 计算函数算 token 数，超过模型上下文窗口则截断实体列表（从尾部删）

**LLM prompt 模板**：
```
你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名。

要求：
- 8个字以下
- 概括这些实体的共同主题
- 不要跟现有脑区重名
- 返回JSON格式：{"label": "标签名"}

现有脑区：{existing_regions}

实体列表：{entity_list}
```

**LLM 调用方式**：复用 `_get_litellm_session(config)` 模式（跟 LightRAG 的 `_llm_model_func` 一致），在 RegionSync 线程里同步调用 `session.chat()`。用主 Agent 同款模型。

**容错机制**：
1. LLM 返回内容先尝试 `json.loads()` 解析，提取 `label` 字段
2. 如果 JSON 解析失败，尝试用正则 `"label"\s*:\s*"([^"]+)"` 提取
3. 如果仍提取失败，或 `label` 为空/超过8字，**重试一次**（同一 prompt 再调一次 LLM）
4. 重试仍失败，fallback 到启发式 `entity_names[0]`
5. 任何情况下都不抛异常，不阻塞 RegionSync 流程

**fallback**：LLM 调用失败时（超时、格式异常、重试仍失败），回退到当前启发式 `entity_names[0]`

**输出处理**：解析成功后，`label` 去前后空白，超过 8 字截断，检查是否跟现有脑区重名（重名则加数字后缀如"编程开发2"）。

### 2. `_encode_description()` — description 改为实体名拼接

**当前**：`summary<SEP>brain_meta_region_id:...<SEP>brain_meta_size:...<SEP>brain_meta_representative:...<SEP>brain_meta_updated_at:...`

其中 summary 是 `"Python(skill)、Django(framework)等3个实体"`

**改为**：summary 部分改为 top-10 实体名用 `<SEP>` 分隔

```
Python<SEP>Django<SEP>FastAPI<SEP>Redis<SEP>Celery<SEP>brain_meta_region_id:community_0<SEP>brain_meta_size:150<SEP>brain_meta_representative:Python<SEP>brain_meta_updated_at:1718380800
```

**为什么**：`<SEP>` 是 LightRAG 的字段分隔符，向量检索时会将分隔后的各段作为独立语义片段。实体名直接放进去，关键词检索就能命中脑区。

**top-10 的选取**：按社区内度数排序（连接数最多的排前面），不是 igraph 顶点 ID 顺序。需要在 `_build_partitions()` 或 `create_region_nodes()` 中计算社区内度数。

### 3. `create_region_nodes()` — 加入 chunks

**当前**：`inject_custom_kg(entities=..., relationships=..., chunks=[])`

**改为**：为每个脑区生成一个 chunk，内容包含脑区标签名 + top 实体名列表

```python
chunks=[{
    "content": f"{region_label}脑区：{', '.join(top_members)}",
    "source_id": f"brain_region:{region_label}",
    "file_path": REGION_FILE_PATH,
}]
```

**为什么**：KG 开发字典明确指出，`chunks=[]` 的实体是"死实体"，不会和已有实体产生语义连接。有 chunk 后，脑区在向量空间可见，大模型处理新内容时能通过语义匹配发现已有脑区并主动建关系。

**注意**：entities、relationships、chunks 必须在同一次 `inject_custom_kg` 调用中传入（KG 开发字典陷阱 #19），否则 source_id 映射会变成 UNKNOWN。

### 4. 社区内度数计算

**当前**：`members[0]` 被注释为 "highest-degree in community"，但实际只是 igraph 顶点 ID 顺序的第一个。

**改为**：在 `_build_partitions()` 中，利用已有的 igraph 图计算每个成员的社区内度数（只统计社区内部边），按度数降序排列 `entity_names`。这样 `entity_names[0]` 才是真正的社区中心实体。

**实现**：在 `_build_partitions()` 的循环中，对每个 community 的成员，用 `igraph.es.select(_within=community_vertices)` 计算每个顶点的社区内度数，按此排序。

## 不改动

- **changelog 事件中的 `entity:` 前缀**：前端和 renderer.js 依赖，不动
- **默认脑区**：`create_default_regions()` 创建的 6 个默认脑区，已有好的命名，不需要 LLM 命名
- **RegionSync 同步间隔**：保持 86400 秒（24小时）
- **Leiden 算法参数**：不动

## 验证标准

1. 创建新脑区时，标签名是语义化的中文（如"编程开发"而非"Python"）
2. 脑区 description 中包含 top-10 实体名，向量检索能命中
3. 脑区有 chunk，`ainsert` 处理新内容时大模型能发现并连接已有脑区
4. LLM 调用失败时 fallback 到启发式命名，不阻塞流程
5. 现有脑区不受影响（只在创建新脑区时走新逻辑）
