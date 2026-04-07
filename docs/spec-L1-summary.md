# L1 摘要层规范

> 版本：v2.0  
> 日期：2026-04-07  
> 更新：新增L2归一化要求、L1内容强制英文  
> 参考：`E:\tmp\L1-Summary-Optimization-Guide.md`

---

## ⚠️ v2.0 核心变更

### 1. L1 内容强制使用英文

**原因**：测试证明英文向量检索相似度显著更高

| 对比项 | 中文查询 | 英文查询 | 提升 |
|--------|---------|---------|------|
| "5分钟后提醒我吃药" | 0.2848 | 0.8276 | **+191%** |
| 工具匹配准确率 | 低 | 高 | **显著提升** |

**要求**：
- 所有工具的 L1 描述必须用英文
- Skills 的 L1 摘要建议用英文
- 文档摘要可保留中文（给用户看）

### 2. 向量 L2 归一化

**要求**：所有入库的 embedding 向量必须做 L2 归一化

**实现**：
```python
import numpy as np

# 入库时
embedding_array = np.array(embedding, dtype=np.float32)
embedding_normalized = embedding_array / np.linalg.norm(embedding_array)
embedding_blob = embedding_normalized.tobytes()
```

**好处**：
1. **计算优化**：余弦相似度简化为点积，无需除以范数
2. **数值稳定性**：所有向量在单位球面上，避免极端值
3. **代码简化**：`score = np.dot(query_vec, doc_vec)` （不再需要归一化）

---

## 一、核心结论

**最优组合：英文内容 + L2归一化 + 极简分隔格式**

| 优化项 | 收益 |
|--------|------|
| 英文内容 | 向量相似度提升 100-200% |
| L2归一化 | 计算优化 + 数值稳定 |
| 极简格式 | Token节省 60% |

**权衡**：
- 英文比中文多消耗 30-40% token
- 但检索准确率提升 100-200%
- **推荐**：向量检索场景优先考虑准确性

---

## 二、L1 存储格式规范

### 2.1 单条存储格式

每条 L1 记录独立存储时，使用极简格式：

```
{Title}|{Keywords}|{Summary}|{Entities}|{Type}|{Pointer}
```

**实际示例（英文）**：
```
Redis Distributed Cache Design|cache,Redis,architecture|Distributed cache system implementation based on Redis|Redis,cache|technical|/docs/cache.md
```

### 2.2 向量存储要求

**格式**：
```python
{
    "id": "doc:xxx",
    "content": "English L1 content for vector search",
    "embedding": [0.123, -0.456, ...],  # L2归一化后的向量
    "metadata": {
        "level": "l1",
        "category": "...",
        "language": "en",  # 标记语言
        "normalized": True  # 标记已归一化
    }
}
```

**关键要求**：
1. ✅ `content` 字段必须用英文（用于向量检索）
2. ✅ `embedding` 向量必须做 L2 归一化
3. ✅ 存储 `normalized: True` 标记

### 2.3 批量返回格式（给 LLM 时）

检索返回多条记录时，在最前面加一行总数：

```
Total: {N}
{Title}|{Keywords}|{Summary}|{Entities}|{Type}|{Pointer}
{Title}|{Keywords}|{Summary}|{Entities}|{Type}|{Pointer}
...
```

**实际示例**：
```
Total: 3
Redis Distributed Cache Design|cache,Redis,architecture|Distributed cache system implementation based on Redis|Redis,cache|technical|/docs/cache.md
API Design Specification|API,REST,authentication|RESTful API design specification and authentication mechanism|API,Token|spec|/docs/api.md
Database Optimization|MySQL,index,performance|MySQL index optimization and query performance tuning strategies|MySQL,index|technical|/docs/db.md
```

### 2.4 字段说明

| 字段 | 长度限制 | 说明 |
|------|---------|------|
| Title | 原文标题 | 英文，保留核心技术术语 |
| Keywords | 3-5个 | 英文关键词，用逗号分隔，支持稀疏检索 |
| Summary | 50-80词 | 英文摘要，信息密度最大化 |
| Entities | 2-5个 | 命名实体，用于精确匹配 |
| Type | 分类标签 | 英文分类，如 technical, spec, guide |
| Pointer | L2位置 | 指向原文的指针（文件路径、数据库ID等） |

### 2.5 L2归一化实现

**代码示例**：
```python
import numpy as np

def normalize_embedding(embedding: list[float]) -> bytes:
    """L2归一化并转换为存储格式"""
    vec = np.array(embedding, dtype=np.float32)
    
    # L2归一化
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec = vec / norm
    
    # 转换为字节
    return vec.tobytes()

# 使用
embedding = get_embedding("English L1 content")
embedding_blob = normalize_embedding(embedding)
```

**检索时的简化**：
```python
# 查询向量也需要归一化
query_vec = np.array(query_embedding, dtype=np.float32)
query_vec = query_vec / np.linalg.norm(query_vec)

# 相似度计算简化为点积
for doc_vec in doc_vectors:
    score = np.dot(query_vec, doc_vec)  # ✅ 无需再除以范数
```

### 2.6 为什么L1用英文？

**测试数据证明**：

| 查询类型 | 中文查询相似度 | 英文查询相似度 | 提升 |
|---------|--------------|--------------|------|
| "5分钟后提醒我吃药" | 0.2848 | 0.8276 | **+191%** |
| "set reminder" | - | 0.5720 | 高准确率 |

**核心原因**：

1. **预训练数据优势**：Multilingual模型的英文预训练数据更多
2. **表达标准化**：英文表达变数小，语义更稳定
   - 中文："5分钟后"、"一会儿"、"待会儿"、"过一哈儿"
   - 英文："in 5 minutes", "remind me in X minutes"（变体少）
3. **工具名匹配**：工具名本身是英文（如 `schedule_task`），英文查询天然匹配
4. **跨语言能力**：Multilingual模型支持跨语言检索，英文向量能匹配中文查询

**Token成本权衡**：
- 英文比中文多消耗 30-40% token
- 但检索准确率提升 100-200%
- **结论**：向量检索场景优先考虑准确性

### 2.7 为什么不需要字段名声明？

---

## 三、Token 效率对比

### 3.1 同一内容的 Token 消耗

**方案A：英文 + JSON（基准）**
```json
{
  "title": "Redis distributed cache design",
  "keywords": ["cache", "Redis", "architecture"],
  "summary": "Implementation of distributed cache system based on Redis",
  "entities": ["Redis", "cache"],
  "type": "technical",
  "point": "/docs/cache.md"
}
```
≈ **55 tokens**

**方案B：中文 + JSON**
```json
{
  "标题": "Redis分布式缓存设计",
  "关键词": ["缓存", "Redis", "架构"],
  "摘要": "基于Redis的分布式缓存系统实现方案",
  "实体": ["Redis", "缓存"],
  "类型": "技术文档",
  "指针": "/docs/cache.md"
}
```
≈ **48 tokens**（节省 13%）

**方案C：中文 + 极简格式（推荐）**
```
Redis分布式缓存设计|缓存,Redis,架构|基于Redis的分布式缓存系统实现方案|Redis,缓存|技术文档|/docs/cache.md
```
≈ **22 tokens**（节省 60%）

### 3.2 批量存储效率

| N条记录 | JSON格式 | 极简格式 | 节省 |
|---------|---------|---------|------|
| 10条 | ~550 tokens | ~220 tokens | 60% |
| 100条 | ~5,500 tokens | ~2,200 tokens | 60% |
| 1000条 | ~55,000 tokens | ~22,000 tokens | 60% |

---

## 四、进阶方案：向量为主，文本为辅

### 4.1 核心思路

既然 L1 主要是给 AI 看的，而向量是 AI 最直接的"理解形式"：

**最优方案 = 纯向量检索 + 最小文本锚点**

### 4.2 极致精简格式

```
{标题}|{指针}
```

**示例**：
```
Redis分布式缓存设计|/docs/cache.md
```
≈ **8 tokens**

### 4.3 流程设计

```
用户查询
    ↓
向量相似度搜索 (L1的embedding)
    ↓
返回Top-K候选的标题
    ↓
LLM读标题判断是否相关（仅5-10 tokens/条）
    ↓
只fetch相关的L2
```

### 4.4 方案对比

| L1格式 | Token/条 | 检索方式 | LLM判断能力 |
|--------|---------|---------|------------|
| 完整摘要 | 30-50 | 向量+文本 | 高 |
| 极简格式 | 20-25 | 向量+文本 | 中 |
| 标题+指针 | 8-10 | 纯向量 | 低（仅靠标题） |

**推荐**：根据召回率要求选择
- 高召回场景：极简格式（标题|关键词|摘要|实体|类型|指针）
- 性能优先场景：标题+指针（向量检索为主）

---

## 五、压缩比例建议

| 压缩比 | Token | 语义保真度 | 推荐度 |
|--------|-------|-----------|--------|
| 3-5x | 原文20-30% | >95% | ⭐⭐⭐⭐⭐ 最优 |
| 5-10x | 原文10-20% | 85-95% | ⭐⭐⭐⭐ 可用 |
| >10x | 原文<10% | <85% | ⭐⭐ 不推荐 |

**建议**：L1 控制在原文的 20-30% token 量

---

## 六、生成 L1 的 Prompt 模板

```
请为以下文档生成L1摘要，格式要求：

1. 标题：保留原标题
2. 关键词：提取3-5个核心概念，用逗号分隔
3. 摘要：50-80字现代中文，包含：
   - 文档主题
   - 核心结论/发现
   - 关键实体名称
4. 实体：提取所有命名实体（人名、地名、机构、技术名词）
5. 类型：归类到预设分类中

输出格式（用|分隔）：
标题|关键词|摘要|实体|类型

原文：
{L2内容}
```

---

## 七、存储架构

```
┌─────────────────────────────────────────────────────┐
│                    L0 (对话核心摘要)                  │
│  最小化：一句话核心信息                               │
│  存储：messages 表                                    │
├─────────────────────────────────────────────────────┤
│                    L1 (摘要层)                       │
│  本规范：向量为主 + 极简文本格式                      │
│  用途：向量检索、LLM上下文判断                        │
│  存储：向量数据库 + KV存储                           │
├─────────────────────────────────────────────────────┤
│                    L2 (全文层)                       │
│  原始内容，完整信息                                  │
│  存储：向量数据库                                    │
└─────────────────────────────────────────────────────┘
```

---

## 八、检索流程

```
用户查询
    ↓
向量相似度搜索 (L1摘要的embedding)
    ↓
稀疏关键词匹配 (L1的关键词/实体)
    ↓
混合排序 (dense + sparse分数融合)
    ↓
返回：共N条 + L1记录
    ↓
LLM读取L1摘要判断相关性
    ↓
按需获取L2全文进行回答
```

---

## 九、关键技术参数

| 参数 | 推荐值 | 说明 |
|------|--------|------|
| 摘要长度 | 50-80字 | 平衡信息量与token |
| 关键词数量 | 3-5个 | 覆盖核心概念 |
| 实体数量 | 2-5个 | 关键命名实体 |
| 向量维度 | 768-1536 | 标准 embedding 维度 |
| 压缩比例 | 3-5x | 最优语义保真 |
| 分隔符 | `\|` | 竖线，避免与内容冲突 |

---

## 十、注意事项

1. **不要用文言文**：虽然 token 更少，但 LLM 理解可能有偏差，向量检索效果不稳定

2. **保留实体名称**：压缩时最容易丢失的是实体，需显式提取存储

3. **技术术语用英文**：如 Redis、API、JSON 等，token 效率更高

4. **避免 JSON 重复字段名**：多条记录时字段名重复消耗大量 token

5. **分隔符选择**：默认用 `|`，如果摘要内容含 `|` 则改用 `;` 或其他

6. **字段顺序固定**：不声明字段名的代价是顺序必须一致，LLM 按位置解析

---

## 十一、参考资料

- [Is Sanskrit the Most Token-Efficient Language? (2026)](https://arxiv.org/html/2601.06142v1)
- [The Linguistic Efficiency of Logograms in LLMs (2026)](https://yujitomita.com/2026/01/09/the-linguistic-efficiency-of-logograms-in-large-language-models/)
- [TOON vs JSON: Token-Optimized Data Format (2025)](https://www.tensorflow.ai/blog/toon-vs-json)
- [Semantic Compression With LLMs (2023)](https://arxiv.org/abs/2304.12512)
- [Do All Languages Cost the Same? (2023)](https://arxiv.org/abs/2305.13705)
