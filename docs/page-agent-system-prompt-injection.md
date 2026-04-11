# Page-Agent 系统提示词注入方案

## 一、原始提示词分析

### 关键可覆盖部分
1. **语言设置** - 已支持 `systemInstruction`
2. **浏览器规则** - 可通过注入覆盖默认行为
3. **任务完成规则** - 可引导它使用我们的知识库

### 关键限制
- 缺省使用 Google 搜索（`<browser_rules>` 中有暗示）
- 无知识库访问能力
- 无自定义工具支持

## 二、注入方案

### 方案 A：轻量级注入（推荐）

在 `systemInstruction` 中注入：

```javascript
const systemInstruction = `
你是一个智能浏览器助手，具备以下增强能力：

## 知识库访问能力

当需要查找信息时，**优先使用本地知识库**：

### 访问方式
通过浏览器访问以下本地API：

1. **知识检索**：
   GET http://localhost:9876/kb/search?q={query}&limit=5
   返回：相关文档列表

2. **知识问答**：
   GET http://localhost:9876/kb/answer?context={context}&question={question}
   返回：基于知识库的答案

### 使用场景
- 遇到专业术语或概念 → 先查知识库
- 需要背景信息 → 先查知识库
- 不确定如何操作 → 先查知识库
- 只有知识库没有答案时，才使用外部搜索

### 优势
- ✅ 本地知识库响应更快
- ✅ 信息更准确、相关
- ✅ 保护隐私（不泄露到外部）

## 工作流程优化

1. **任务分解**：复杂任务自动拆分为小步骤
2. **快速反馈**：每步操作及时返回结果
3. **错误恢复**：遇到问题立即报告，不要长时间重试

## 示例工作流

任务："完成MBTI人格测试"

正确流程：
1. 打开测试页面
2. 获取第一题 → 发现需要理解题目背景
3. 访问知识库：GET http://localhost:9876/kb/search?q=MBTI人格测试
4. 根据知识库信息选择答案
5. 继续下一题
6. 完成后调用 \`done\` 返回结果

## 语言偏好

- Default working language: **中文**
- 用户用什么语言提问，就用什么语言回答
`
```

### 方案 B：深度注入

如果需要完全控制，可以注入更详细的规则：

```javascript
const systemInstruction = `
[覆盖原始提示词中的浏览器规则]

<browser_rules_override>
## 搜索优先级

1. **本地知识库（最高优先级）**
   - URL: http://localhost:9876/kb/search
   - 适用：专业知识、概念解释、操作指南
   - 方法：浏览器直接访问该地址

2. **外部搜索（次优先级）**
   - 仅在知识库无结果时使用
   - 避免频繁访问，保护隐私

## 工作模式

### 交互式任务（如MBTI测试）
- 每个步骤都要及时返回进度
- 遇到不确定的问题 → 先查知识库
- 根据知识库信息决策
- 不要长时间尝试多种方法

### 信息收集任务
- 优先从本地服务获取信息
- 整理后结构化返回

### 表单填写任务
- 分步骤完成
- 每个字段填写后验证
- 快速报告错误

## 本地服务集成

### 知识库 API
Base URL: http://localhost:9876/kb

端点：
- GET /search?q={query} - 检索知识
- GET /answer?context={context} - 生成答案
- GET /health - 检查服务状态

使用示例：
1. 浏览器访问：http://localhost:9876/kb/search?q=MBTI测试
2. 解析返回的 JSON 数据
3. 根据数据继续任务

## 错误处理

- 知识库访问失败 → fallback 到外部搜索
- 页面加载超时 → 立即报告，不长时间等待
- 元素找不到 → 滚动页面或重新导航
</browser_rules_override>
`
```

## 三、实施步骤

### Step 1: 修改 index.js

```javascript
// mcp-servers/page-agent-mcp/src/index.js
// 第 166-173 行

const proxyConfig = {
    baseURL: 'http://localhost:9876/proxy/v1',
    model: 'local',
    apiKey: 'local',
    systemInstruction: `
你是一个智能浏览器助手，具备本地知识库访问能力。

## 知识库使用（最高优先级）

需要信息时，**优先访问本地知识库**：
- 检索：http://localhost:9876/kb/search?q={query}
- 问答：http://localhost:9876/kb/answer?context={context}

只有知识库无结果时，才使用外部搜索。

## 工作原则

1. 快速反馈：每步操作及时返回结果
2. 遇到困难：立即报告，不要长时间重试
3. 任务拆分：复杂任务自动分解为小步骤
4. 本地优先：优先使用本地服务（更快、更准确）

## 语言

Default working language: **中文**
按用户使用的语言回复。
`
}

const result = await hub.executeTask(task, proxyConfig)
```

### Step 2: 实现知识库 API

在 `niu_api/` 中添加端点：

```python
# niu_api/kb.py

from fastapi import APIRouter, Query
from typing import List, Dict
import json

router = APIRouter(prefix="/kb", tags=["knowledge-base"])

@router.get("/search")
async def search_knowledge(
    q: str = Query(..., description="搜索关键词"),
    limit: int = Query(5, description="返回结果数量")
):
    """
    知识库搜索
    TODO: 对接实际的向量检索
    """
    # 临时返回模拟数据
    return {
        "success": True,
        "results": [
            {
                "title": "MBTI人格测试简介",
                "content": "MBTI是Myers-Briggs Type Indicator的缩写...",
                "relevance": 0.95
            }
        ]
    }

@router.get("/answer")
async def answer_question(
    context: str = Query(..., description="上下文"),
    question: str = Query(..., description="问题")
):
    """
    基于知识库回答问题
    TODO: 对接实际的RAG系统
    """
    return {
        "success": True,
        "answer": "根据知识库信息...",
        "sources": ["doc1", "doc2"]
    }

@router.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "ok"}
```

### Step 3: 注册路由

```python
# niu_api/main.py

from niu_api.kb import router as kb_router

app.include_router(kb_router)
```

## 四、测试计划

### 测试 1：基础知识库访问

```python
# scripts/test_kb_access.py

import requests

# 测试知识库API
response = requests.get("http://localhost:9876/kb/search", params={
    "q": "MBTI人格测试"
})

print(response.json())
```

### 测试 2：Page-Agent 使用知识库

```python
# scripts/test_page_agent_with_kb.py

from niu_page_agent import execute_task

# 任务：需要知识库支持
result = execute_task("""
完成MBTI人格测试：
1. 打开 https://mbti-test.app/zh-cn/free-personality-test
2. 遇到不确定的问题，先查知识库
3. 根据知识库信息选择答案
4. 完成整个测试
""")

print(result)
```

## 五、预期效果

### 之前（无知识库）
```
主Agent: "完成MBTI测试"
↓
Page-Agent: 盲目答题，结果不可控
```

### 之后（有知识库）
```
主Agent: "完成MBTI测试"
↓
Page-Agent:
1. 打开页面
2. 获取题目
3. 查知识库：http://localhost:9876/kb/search?q=MBTI外向内向维度
4. 根据知识回答
5. 继续下一题
6. 完成
```

## 六、优势

✅ **不改扩展代码** - 只注入提示词
✅ **完全可控** - 决定它如何工作
✅ **知识库集成** - 复用 ai-bot 现有能力
✅ **异步友好** - 可以提交大任务让它自己完成
✅ **隐私保护** - 敏感信息不泄露到外部

## 七、后续优化

1. **对接真实知识库**
   - 集成向量检索
   - 支持文档、笔记、代码

2. **多模态支持**
   - 图片理解（OCR）
   - 文件处理

3. **异步任务管理**
   - 任务队列
   - 进度跟踪
   - 结果缓存

## 八、风险与缓解

| 风险 | 缓解措施 |
|------|---------|
| 提示词过长影响性能 | 使用精简版本 |
| 知识库服务不可用 | 优雅降级到外部搜索 |
| 注入的规则被忽略 | 多次强调、提供示例 |

---

**总结**：通过 systemInstruction 注入，我们可以完全控制 page-agent 的行为，让它使用我们的知识库、遵循我们的规则，实现真正的"自主智能浏览器助手"。
