# 脑区 LLM 命名改进设计

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将脑区命名从启发式（取第一个实体名）改为 LLM 生成语义化名称，同时修复 description 格式和 chunks 缺失问题，使脑区可被向量检索命中。

**Architecture:** 拆分 `_summarize_region()` 为 `_generate_region_label()`（LLM）和 `_generate_region_summary()`（启发式）；description 改为 top-10 实体名 `<SEP>` 拼接；`inject_custom_kg` 带实体级 source_id 匹配的 chunks。

**Tech Stack:** LiteLLMSession、litellm.token_counter

---

## 现状问题

1. **命名无语义**：`_summarize_region()` 取 `entity_names[0]`（igraph 顶点 ID 最小的成员）作为脑区标签，跟社区主题无关
2. **description 不可检索**：格式为 `"Python(skill)、Django(framework)等3个实体<SEP>brain_meta_..."` ，向量检索无法命中
3. **chunks=[] 导致死实体**：`create_region_nodes()` 调用 `inject_custom_kg(entities=..., relationships=..., chunks=[])` ，脑区在向量空间不可见，大模型无法主动跟它建关系

## 改动范围

### 1. 拆分 `_summarize_region()` 为两个方法

**当前**：`_summarize_region(entity_summaries) -> (label, summary)` 一个方法同时生成标签和摘要

**改为**：

- `_generate_region_label(entity_summaries, existing_regions) -> str`：LLM 生成标签名
- `_generate_region_summary(entity_names) -> str`：启发式生成摘要（top-10 实体名 `<SEP>` 拼接）

**为什么拆分**：`update_region_summaries()` 只需要更新摘要，不需要重新生成标签。如果不拆分，每次更新摘要都会触发一次无用的 LLM 调用，24小时同步一次就是 N 次浪费。

**调用关系**：
- `create_region_nodes()`：调用 `_generate_region_label()` + `_generate_region_summary()`
- `update_region_summaries()`：只调用 `_generate_region_summary()`

### 2. `_generate_region_label()` — LLM 生成标签名

**输入组装**：
- 社区内实体名+类型列表：`"Python(skill), Django(framework), FastAPI(framework), ..."`
- 现有脑区标签列表（避免重名）：从 `RegionManager.get_all_regions()` 获取
- 组装完后用 `litellm.token_counter(model=model_name, text=prompt_text)` 算 token 数，超过模型上下文窗口则截断实体列表（从尾部删）
- 上下文窗口大小从 `~/.niu/preferences.json` 的 `context.contextWindowSize` 读取（已有函数 `_read_context_window_tokens()` 在 `agent/subagent.py`）

**LLM prompt 模板**：
```
你是一个知识图谱分析师。根据以下社区内的实体列表，为这个社区生成一个简洁的中文标签名。

要求：
- 8个字以下
- 概括这些实体的共同主题
- 不要跟现有脑区重名或语义接近
- 只能返回JSON格式：{"label": "标签名"}
- 返回其他任何格式或内容将判定失败

现有脑区：{existing_regions}

实体列表：{entity_list}
```

**LLM 调用方式**：

```python
from niu_api.internal.lightrag_manager import _get_litellm_session
from niu_api.llm_proxy import get_llm_config

config = get_llm_config()  # 主 Agent 同款模型
session = _get_litellm_session(config)
gen = session.chat(messages=[{"role": "user", "content": prompt}])

# 消费 generator，提取完整文本
chunks = []
mock_response = None
try:
    while True:
        chunk = next(gen)
        if isinstance(chunk, str):
            chunks.append(chunk)
except StopIteration as e:
    mock_response = e.value
full_content = "".join(chunks)
```

**单次超时**：30 秒（LLM 只需生成几个字，不需要 120 秒默认超时）

**容错机制**：
1. LLM 返回内容先尝试 `json.loads()` 解析，提取 `label` 字段
2. 如果 JSON 解析失败，尝试用正则 `"label"\s*:\s*"([^"]+)"` 提取
3. 如果仍提取失败，或 `label` 为空/超过8字，**重试一次**（同一 prompt 再调一次 LLM）
4. 重试仍失败，fallback 到启发式 `entity_names[0]`
5. 任何情况下都不抛异常，不阻塞 RegionSync 流程

**输出处理**：解析成功后，`label` 去前后空白，超过 8 字截断，检查是否跟现有脑区重名（重名则加数字后缀如"编程开发2"）。

### 3. `_generate_region_summary()` — description 改为实体名拼接

**当前**：summary 是 `"Python(skill)、Django(framework)等3个实体"`

**改为**：top-10 实体名用 `<SEP>` 分隔

```
Python<SEP>Django<SEP>FastAPI<SEP>Redis<SEP>Celery<SEP>brain_meta_region_id:community_0<SEP>brain_meta_size:150<SEP>brain_meta_representative:Python<SEP>brain_meta_updated_at:1718380800
```

**为什么**：`<SEP>` 是 LightRAG 的字段分隔符，向量检索时会将分隔后的各段作为独立语义片段。实体名直接放进去，关键词检索就能命中脑区。

**top-10 的选取**：按社区内度数排序（连接数最多的排前面），不是 igraph 顶点 ID 顺序。

**现有脑区迁移**：`update_region_summaries()` 会在下次 sync 时用新格式覆盖旧脑区的 description，旧格式自动升级，无需单独迁移。

### 4. `create_region_nodes()` — 加入 chunks

**当前**：`inject_custom_kg(entities=..., relationships=..., chunks=[])`

**改为**：为每个脑区生成一个 chunk，内容包含脑区标签名 + top 实体名列表

**关键**：必须确保 chunk 的 `source_id` 覆盖 entity 的 `source_id`，避免 `inject_custom_kg` 的自动虚拟 chunk 机制生成重复内容。具体做法：

```python
# 每个 entity 的 source_id 设为 chunk 的 source_id
chunk_source_id = f"brain_region:{region_label}"

all_entities.append({
    "entity_name": region_name,
    "entity_type": REGION_ENTITY_TYPE,
    "description": description,
    "source_id": chunk_source_id,  # 跟 chunk 对齐
})

# chunk 内容
chunks=[{
    "content": f"{region_label}脑区：{', '.join(top_members)}",
    "source_id": chunk_source_id,  # 跟 entity 对齐
    "file_path": REGION_FILE_PATH,
}]
```

这样 `chunk_to_source_map[chunk_source_id] = chunk_id`，entity 的 `source_id` 能在 map 中找到对应 chunk，不会触发虚拟 chunk 生成。

**注意**：entities、relationships、chunks 必须在同一次 `inject_custom_kg` 调用中传入（KG 开发字典陷阱 #19），否则 source_id 映射会变成 UNKNOWN。

### 5. 社区内度数计算

**当前**：`members[0]` 被注释为 "highest-degree in community"，但实际只是 igraph 顶点 ID 顺序的第一个。

**改为**：在 `_build_partitions()` 中，利用 igraph 的子图功能计算社区内度数，按度数降序排列 `entity_names`：

```python
# 对每个社区
member_vids = [v for v in membership if membership[v] == community_id]
subgraph = ig.subgraph(member_vids)
degrees = subgraph.degree()
# 按 degree 降序排列 member_vids
sorted_pairs = sorted(zip(member_vids, degrees), key=lambda x: x[1], reverse=True)
sorted_names = [node_names[vid] for vid, _ in sorted_pairs]
```

这样 `entity_names[0]` 才是真正的社区中心实体，`representative` 和 description 中的 top-10 实体也都按此排序。

### 6. 批量 LLM 调用性能

`create_region_nodes()` 对每个 partition 循环调用 `_generate_region_label()`，每个脑区一次 LLM 调用。以每次 3-10 秒计算，5 个脑区需要 15-50 秒。

**方案**：如果一次同步检测到 3 个以上新脑区，将所有社区数据合并到一个 prompt 中，一次 LLM 调用生成所有标签：

```
你是一个知识图谱分析师。根据以下社区内的实体列表，为每个社区生成一个简洁的中文标签名。

要求：
- 每个标签8个字以下
- 概括该社区实体的共同主题
- 不要跟现有脑区重名或语义接近
- 只能返回JSON格式：{"regions": [{"id": 0, "label": "标签1"}, ...]}
- 返回其他任何格式或内容将判定失败

现有脑区：...

社区0实体：Python(skill), Django(framework), ...
社区1实体：任飞(person), 李明(person), ...
社区2实体：雄安分行(organization), 河北分行(organization), ...
```

3 个以下则逐个调用（避免批量 prompt 格式不稳定）。

## 不改动

- **changelog 事件中的 `entity:` 前缀**：前端和 renderer.js 依赖，不动
- **默认脑区**：`create_default_regions()` 创建的 6 个默认脑区，已有好的命名，不需要 LLM 命名。其 `inject_custom_kg(chunks=[])` 的问题由虚拟 chunk 机制自动兜底。
- **RegionSync 同步间隔**：保持 86400 秒（24小时）
- **Leiden 算法参数**：不动

## 验证标准

1. 创建新脑区时，标签名是语义化的中文（如"编程开发"而非"Python"）
2. 脑区 description 中包含 top-10 实体名，向量检索能命中
3. 脑区有 chunk，`ainsert` 处理新内容时大模型能发现并连接已有脑区
4. LLM 调用失败时 fallback 到启发式命名，不阻塞流程
5. `update_region_summaries()` 不触发 LLM 调用，只更新 description 格式
6. 旧格式 description 在下次 sync 时自动升级为新格式
