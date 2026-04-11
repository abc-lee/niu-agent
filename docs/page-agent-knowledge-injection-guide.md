# Page-Agent 知识库注入机制 - 使用指南

## 🎯 核心原理

**问题**：Page-Agent 只有浏览器操作能力，无法直接访问知识库 API。

**解决方案**：在 MCP Server 层面预处理任务，自动注入知识库内容。

```
用户任务："完成MBTI人格测试"
    ↓
MCP Server 检测关键词 "MBTI"
    ↓
调用知识库 API：http://localhost:9876/kb/search?q=MBTI
    ↓
获取知识库内容（MBTI理论、维度详解等）
    ↓
注入到任务描述中：
    "完成MBTI人格测试
     ---
     【知识库参考】
     MBTI（Myers-Briggs Type Indicator）是一种人格类型指标...
     外向型特征：从外部世界获得能量...
     内向型特征：从内心世界获得能量..."
    ↓
Page-Agent 收到增强后的任务
    ↓
根据注入的知识库内容完成测试
```

---

## 📋 已完成的修改

### 1. 知识库查询函数 ✅

**文件**：`mcp-servers/page-agent-mcp/src/index.js`

```javascript
/**
 * 查询知识库并返回相关内容
 */
async function queryKnowledgeBase(query) {
    const KB_API_BASE = 'http://localhost:9876/kb'

    const url = `${KB_API_BASE}/search?q=${encodeURIComponent(query)}&limit=3`
    const response = await fetch(url, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
        signal: AbortSignal.timeout(3000) // 3秒超时
    })

    const data = await response.json()
    return data.results.map(r => `${r.title}\n${r.content}`).join('\n\n')
}
```

### 2. 任务增强函数 ✅

```javascript
/**
 * 增强任务描述：注入知识库内容
 */
async function enhanceTaskWithKnowledge(task) {
    // 1. 提取查询关键词
    const queries = extractKnowledgeQueries(task)

    // 2. 查询知识库
    const knowledgeParts = []
    for (const query of queries) {
        const knowledge = await queryKnowledgeBase(query)
        if (knowledge) {
            knowledgeParts.push(`【${query}】\n${knowledge}`)
        }
    }

    // 3. 注入知识库内容到任务描述
    return `
${task}

---

【知识库参考】
以下是相关知识库内容，请参考这些信息完成任务：

${knowledgeParts.join('\n\n---\n\n')}
`
}
```

### 3. execute_task 集成 ✅

```javascript
mcpServer.registerTool(
    'execute_task',
    { ... },
    async ({ task }) => {
        try {
            // 1. 增强任务：注入知识库内容
            const enhancedTask = await enhanceTaskWithKnowledge(task)

            // 2. 执行任务
            const result = await hub.executeTask(enhancedTask, config)

            return { ... }
        } catch (err) {
            return { ... }
        }
    }
)
```

### 4. 知识库 API 扩展 ✅

**文件**：`niu_api/kb.py`

- ✅ 支持 MBTI 相关查询
- ✅ 支持外向/内向维度查询
- ✅ 支持浏览器自动化查询
- ✅ 返回详细的知识内容

---

## 🧪 测试方法

### 测试 1：验证知识库注入

**启动服务**：
```bash
# 启动 ai-bot API（包含知识库）
python -m niu_api

# 启动 MCP Server（会自动启动）
# 在另一个终端查看日志
tail -f E:/tools/ai-bot/logs/api_stderr.log | grep "kb-"
```

**测试任务**：
```python
# scripts/test_kb_injection.py

from mcp import Client

client = Client("http://localhost:38401")

# 任务包含 "MBTI" 关键词
result = client.execute_task("""
打开百度首页
搜索 "MBTI人格测试"
返回搜索结果页面的标题
""")

print(result)
```

**预期日志**：
```
[kb-enhance] Detected knowledge queries: MBTI
[kb-query] Found 2 results for: MBTI
[kb-enhance] Enhanced task with 1 knowledge items
```

### 测试 2：查看注入的任务内容

**修改 index.js，添加日志输出**：
```javascript
async ({ task }) => {
    try {
        const enhancedTask = await enhanceTaskWithKnowledge(task)

        // 输出增强后的任务（调试）
        console.error('[enhanced-task]\n' + enhancedTask)

        const result = await hub.executeTask(enhancedTask, config)
        return { ... }
    }
}
```

**查看日志**：
```bash
tail -f E:/tools/ai-bot/logs/api_stderr.log | grep -A 20 "enhanced-task"
```

**预期输出**：
```
[enhanced-task]
打开百度首页
搜索 "MBTI人格测试"
返回搜索结果页面的标题

---

【知识库参考】
以下是相关知识库内容，请参考这些信息完成任务：

【MBTI】
MBTI人格测试简介
MBTI（Myers-Briggs Type Indicator）是一种人格类型指标，基于卡尔·荣格的心理类型理论。

MBTI 将人格分为四个维度：
1. 外向(E) vs 内向(I) - 能量来源
   - 外向型：从外部世界获得能量，喜欢社交、表达、行动
   - 内向型：从内心世界获得能量，喜欢独处、思考、深度
...
```

### 测试 3：完整的 MBTI 测试任务

```python
# scripts/test_mbti_with_kb.py

from mcp import Client

client = Client("http://localhost:38401")

# 任务：完成 MBTI 测试（需要知识库支持）
result = client.execute_task("""
打开 https://mbti-test.app/zh-cn/free-personality-test

完成整个 MBTI 人格测试：
1. 阅读每一题
2. 根据题目内容选择最符合的选项
3. 完成所有题目
4. 返回测试结果（人格类型）
""")

print("测试结果：", result)
```

**工作流程**：
1. Page-Agent 收到任务 + 注入的 MBTI 知识
2. 打开测试页面
3. 获取第一题："在社交场合中，我的感受是..."
4. 根据注入的知识库内容（外向/内向特征）理解题目
5. 选择合适的答案
6. 继续下一题
7. 完成整个测试
8. 返回结果

---

## 🎨 支持的关键词

当前支持自动查询知识库的关键词：

| 关键词 | 匹配规则 | 知识库内容 |
|--------|----------|-----------|
| MBTI | `/MBTI/i` | MBTI 理论、维度详解 |
| 人格测试 | `/人格测试/` | 人格测试方法、类型 |
| 外向/内向 | `/外向\|内向/` | 外向/内向维度详解 |
| 浏览器自动化 | `/浏览器自动化/` | 自动化工具介绍 |
| Page-Agent | `/Page-Agent/i` | Page-Agent 功能介绍 |
| 知识库 | `/知识库/` | 知识库使用方法 |
| RAG | `/RAG/i` | RAG 技术介绍 |
| 向量检索 | `/向量检索/` | 向量检索原理 |

**扩展关键词**：

编辑 `mcp-servers/page-agent-mcp/src/index.js`：

```javascript
function extractKnowledgeQueries(task) {
    const queries = []

    const patterns = [
        /MBTI/i,
        /人格测试/i,
        /外向|内向/i,
        /浏览器自动化/i,
        /Page-Agent/i,
        /知识库/i,
        /RAG/i,
        /向量检索/i,
        // 添加新的关键词
        /你的新关键词/i,
    ]

    for (const pattern of patterns) {
        const match = task.match(pattern)
        if (match) {
            queries.push(match[0])
        }
    }

    return queries
}
```

---

## 📊 架构对比

### 旧方案（失败）

```
提示词告诉 Page-Agent：
"遇到不懂的内容，访问知识库 API"
    ↓
Page-Agent 尝试调用 HTTP 请求
    ↓
失败：Page-Agent 只有浏览器操作能力，无法发起 HTTP 请求
```

### 新方案（成功）

```
MCP Server 预处理：
1. 检测任务中的关键词
2. 查询知识库 API
3. 注入知识内容到任务描述
    ↓
Page-Agent 收到增强后的任务
    ↓
成功：根据注入的知识库内容完成任务
```

---

## 🔧 故障排查

### 问题 1：没有看到知识库注入日志

**检查**：
```bash
# 1. 确认 niu_api 已启动
curl http://localhost:9876/kb/health

# 2. 确认知识库 API 可访问
curl "http://localhost:9876/kb/search?q=MBTI"

# 3. 查看 MCP Server 日志
tail -f E:/tools/ai-bot/logs/api_stderr.log | grep "kb-"
```

**解决**：
- 确保 `python -m niu_api` 正在运行
- 检查端口 9876 未被占用
- 查看 `niu_api/__main__.py` 是否注册了 `kb_router`

### 问题 2：知识库内容不准确

**检查知识库数据**：
```bash
# 测试知识库 API
curl "http://localhost:9876/kb/search?q=MBTI" | jq
```

**更新知识库数据**：
编辑 `niu_api/kb.py`，修改 `mock_results`。

### 问题 3：注入的内容没有被使用

**检查提示词**：
```javascript
// mcp-servers/page-agent-mcp/src/index.js
const SYSTEM_PROMPTS = {
    knowledge_enhanced: `
你是一个智能浏览器助手，具备本地知识库访问能力。

## 已注入的知识库内容

任务描述中已经包含了相关知识库内容，你可以直接使用这些信息：
- 知识库内容会以 "【知识库参考】" 开头
- 包含相关的背景信息、定义、操作指南等
- 请根据这些信息完成任务
...
`
}
```

---

## 🚀 下一步优化

### 1. 对接真实知识库

当前使用模拟数据，下一步对接真实向量检索：

```python
# niu_api/kb.py

@router.get("/search")
async def search_knowledge(q: str, limit: int = 5):
    # TODO: 对接真实向量检索
    from agent.vector_search import search_similar

    results = await search_similar(q, top_k=limit)
    return {
        "success": True,
        "results": results
    }
```

### 2. 智能关键词提取

使用 NLP 提取关键词，而不是硬编码正则：

```javascript
import natural from 'natural'

function extractKnowledgeQueries(task) {
    const tokenizer = new natural.WordTokenizer()
    const tokens = tokenizer.tokenize(task)

    // 提取关键词（名词、专业术语）
    const keywords = extractKeywords(tokens)
    return keywords
}
```

### 3. 多轮对话记忆

记住之前的查询结果，避免重复查询：

```javascript
const knowledgeCache = new Map()

async function queryKnowledgeBase(query) {
    if (knowledgeCache.has(query)) {
        return knowledgeCache.get(query)
    }

    const knowledge = await fetch(...)
    knowledgeCache.set(query, knowledge)
    return knowledge
}
```

---

## 📚 相关文档

- `docs/page-agent-implementation-complete.md` - 完整实施文档
- `docs/page-agent-system-prompt-injection.md` - 注入方案设计
- `scripts/test_page_agent_kb.py` - 测试脚本

---

## ✅ 总结

通过 **MCP Server 层面的知识库预处理和任务增强**，我们实现了：

1. ✅ Page-Agent 能够使用本地知识库内容
2. ✅ 无需修改扩展源码
3. ✅ 自动化知识注入流程
4. ✅ 灵活的关键词扩展机制

**核心价值**：让 Page-Agent 能够基于你的本地知识完成任务，保护隐私、提高准确性、减少外网依赖！🎉
