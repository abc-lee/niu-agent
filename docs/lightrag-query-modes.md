# LightRAG 查询模式技术文档

> 调研日期：2026-04-27
> 源码版本：lightrag-hku 0.x（`E:\opencode\venv\Lib\site-packages\lightrag\`）

## 1. 概述

LightRAG 提供了 6 种查询模式，核心区别在于**是否调用 LLM** 以及**如何组合向量搜索与图遍历**。

关键发现：**所有模式都支持预提供关键词跳过 LLM 调用**，这是实现高性能图检索的关键。

## 2. 六种查询模式

### 2.1 模式总览

| mode | LLM调用 | 内部流程 | 返回内容 | 性能 | 适用场景 |
|------|---------|---------|---------|------|---------|
| `local` | 1-2次 | 关键词→向量搜实体→图遍历取关联→LLM总结 | entities + relationships + chunks + 总结 | 中 | 局部实体查询 |
| `global` | 1-2次 | 关键词→社区报告排序→LLM总结 | entities + relationships + chunks + 总结 | 中 | 全局概览查询 |
| `hybrid` | 2-3次 | local + global → LLM合并总结 | entities + relationships + chunks + 总结 | 慢 | 综合查询 |
| `naive` | 0-1次 | 直接向量搜索chunk→可选LLM总结 | chunks only（无entities/relationships） | 快 | 纯语义搜索 |
| `mix` | 3次 | naive + local + global → LLM合并 | entities + relationships + chunks + 总结 | 最慢 | 最全面查询 |
| `bypass` | 0次 | 不检索，直接返回空 | 空结构 | 极快 | 仅用于 aquery() 让LLM直接回答 |

### 2.2 各模式详细说明

#### local 模式
1. 提取关键词（LLM 或预提供）
2. 用 `ll_keywords` 向量搜索实体节点
3. 从匹配实体出发，图遍历获取关联节点和边
4. 收集关联 chunks
5. 可选：LLM 总结（`only_need_context=True` 时跳过）

**返回**：以实体为中心的局部子图，包含实体属性和关系。

#### global 模式
1. 提取关键词（LLM 或预提供）
2. 用 `hl_keywords` 匹配社区报告（community reports）
3. 排序选取最相关的社区
4. 收集社区内的实体和 chunks
5. 可选：LLM 总结

**返回**：以社区为中心的全局视图，适合宏观理解。

#### hybrid 模式
1. 同时执行 local + global
2. 合并去重结果
3. LLM 合并总结（额外 1 次 LLM 调用）

**返回**：local + global 的并集，最全面但最慢。

#### naive 模式
1. 直接用查询文本向量搜索文档块（chunks）
2. 不经过知识图谱，不返回 entities 和 relationships
3. 可选：LLM 总结

**返回**：只有 chunks，丢失了图谱的结构化信息（entity_type、关系等）。

#### mix 模式
1. 同时执行 naive + local + global
2. 合并去重
3. LLM 合并总结

**返回**：最全面，但 LLM 调用最多（3次）。

#### bypass 模式
1. 不做任何检索
2. `aquery_data()` 返回空结构
3. `aquery()` 直接让 LLM 用自身知识回答

**返回**：`aquery_data()` 返回空，`aquery()` 返回 LLM 直接回答。**在 `aquery_data()` 路径下无意义。**

## 3. 关键参数组合

### 3.1 QueryParam 关键字段

```python
from lightrag.base import QueryParam

param = QueryParam(
    mode="local",              # 查询模式
    top_k=20,                  # 返回结果数量
    hl_keywords=["关键词"],     # 高层关键词（用于 global 模式匹配社区）
    ll_keywords=["关键词"],     # 低层关键词（用于 local 模式向量搜索实体）
    only_need_context=True,    # 跳过最终 LLM 总结，只返回原始数据
)
```

### 3.2 预提供关键词跳过 LLM

**核心机制**：`QueryParam` 的 `hl_keywords` 和 `ll_keywords` 如果非空，`extract_keywords_only()` 直接返回它们，**不调用 LLM**。

源码逻辑（`operate.py`）：
```python
async def extract_keywords_only(query, ...):
    if hl_keywords or ll_keywords:
        return hl_keywords, ll_keywords  # 直接返回，跳过 LLM
    # 否则调用 LLM 提取...
```

**这意味着**：我们可以用用户的查询词直接作为关键词，跳过 LLM 提取步骤，同时保留完整的图遍历能力。

### 3.3 `only_need_context` 参数

- `True`：跳过最终 LLM 总结步骤，只返回原始检索数据
- `False`（默认）：LLM 对检索结果生成自然语言总结

在注入场景下，我们只需要原始数据（entities、relationships、chunks），不需要 LLM 总结。

## 4. 推荐方案

### 4.1 注入场景（_inject_dynamic_resources）

**需求**：快速获取相关 skills/tools/knowledge，按 entity_type 分类，注入到 system prompt。

**方案**：`local` 模式 + 预提供关键词 + `only_need_context=True`

```python
param = QueryParam(
    mode="local",
    ll_keywords=[query],           # 用户查询直接作为低层关键词
    hl_keywords=[query],           # 同时作为高层关键词
    only_need_context=True,        # 跳过 LLM 总结
    top_k=20
)
result = await rag.aquery_data(query, param=param)
```

**效果**：
- LLM 调用：0 次（原来 hybrid 模式 3 次）
- 保留：entities（带 entity_type）、relationships、chunks
- 丢失：LLM 生成的自然语言总结（注入场景不需要）
- 预计耗时：<1s（原来 ~106s）

### 4.2 MCP 工具场景（lightrag_query_data）

**需求**：主 Agent 主动查询知识图谱，可能需要不同粒度的结果。

**方案**：提供 mode 选择，默认 `local`，支持预提供关键词

| 场景 | 推荐 mode | 说明 |
|------|----------|------|
| 查找特定实体/技能/工具 | `local` | 精确匹配实体和关系 |
| 了解全局概览 | `global` | 社区级别宏观理解 |
| 综合查询 | `hybrid` | local + global 合并 |
| 纯语义搜索 | `naive` | 不需要图谱结构时 |
| 快速查询（跳过LLM） | `local` + 预提供关键词 | 0 次 LLM 调用 |

### 4.3 关键词策略

| 查询类型 | hl_keywords | ll_keywords | 说明 |
|---------|-------------|-------------|------|
| 简单关键词（"便签"） | ["便签"] | ["便签"] | 直接用查询词 |
| 复合查询（"查看便签内容"） | ["便签"] | ["便签", "内容"] | 提取核心词 |
| 英文查询（"sticky note"） | ["sticky note"] | ["sticky", "note"] | 拆分词组 |
| 不确定时 | [] | [] | 让 LLM 提取（回退方案） |

## 5. 返回值结构

### 5.1 aquery_data() 返回结构

```json
{
  "status": "success",
  "data": {
    "entities": [
      {
        "entity_name": "note-management",
        "entity_type": "skill",
        "description": "便签管理技能，用于创建、读取、更新、删除便签",
        "source_id": "doc-xxx",
        "file_path": "skill://note-management"
      }
    ],
    "relationships": [
      {
        "src_id": "note-management",
        "tgt_id": "memory-server",
        "description": "便签管理依赖记忆服务存储数据",
        "keywords": "depends_on",
        "weight": 1.0,
        "source_id": "doc-xxx"
      }
    ],
    "chunks": [
      {
        "content": "note-management: Use when user asks to create, read, update, or delete sticky notes...",
        "file_path": "skill://note-management",
        "chunk_id": "chunk-xxx",
        "full_doc_id": "doc-xxx"
      }
    ],
    "references": [
      {
        "source_id": "doc-xxx",
        "content": "原始文档内容..."
      }
    ]
  },
  "metadata": {
    "query_mode": "local",
    "keywords": {
      "high_level": ["便签"],
      "low_level": ["便签"]
    },
    "processing_info": {
      "total_chunks_found": 20,
      "final_chunks_count": 20
    }
  }
}
```

### 5.2 无结果时的返回

```json
{
  "status": "success",
  "data": {
    "entities": [],
    "relationships": [],
    "chunks": [],
    "references": []
  },
  "metadata": { ... }
}
```

注意：即使无结果，`status` 仍然是 `"success"`。需要通过检查 `entities`/`chunks` 是否为空来判断。

### 5.3 错误时的返回

```json
{
  "status": "failure",
  "data": null,
  "metadata": { ... }
}
```

或异常时返回 `None`。

## 6. entity_type 分类映射

LightRAG 图谱中的 `entity_type` 字段用于区分不同类型的实体：

| entity_type | 含义 | 对应注入分类 |
|-------------|------|-------------|
| `skill` | 技能描述 | skill 注入 |
| `tool` / `mcp_tool` | MCP 工具 | mcp_tool 注入 |
| `knowledge` / `concept` | 知识概念 | knowledge 注入 |
| 其他 | 未分类 | knowledge 注入（兜底） |

## 7. 性能对比

| 方案 | LLM调用 | 图遍历 | entity_type | 耗时 |
|------|---------|--------|-------------|------|
| hybrid（当前） | 3次 | ✅ | ✅ | ~106s |
| local + LLM关键词 | 1-2次 | ✅ | ✅ | ~10s |
| local + 预提供关键词 | 0次 | ✅ | ✅ | <1s |
| naive | 0次 | ❌ | ❌ | ~7s |
| bypass | 0次 | ❌ | ❌ | 0s |

**结论**：`local + 预提供关键词 + only_need_context=True` 是注入场景的最优方案，保留了图遍历的全部能力，同时消除了 LLM 调用的延迟。
