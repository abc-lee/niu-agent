# Page-Agent 系统提示词注入 - 实施完成

## 一、核心目标

### 目标 1：异步自主工作模式
```
主 Agent → 提交任务 → 不等待，继续工作
              ↓
         Page-Agent 自主完成（几分钟）
              ↓
         返回结果 → 主 Agent → 反馈用户
```

### 目标 2：本地知识库集成
```
Page-Agent 遇到不懂的内容
    ↓
访问本地知识库（http://localhost:9876/kb/search）
    ↓
根据知识库完成任务（不依赖外网）
```

## 二、实施方案

### 方案：动态系统提示词注入

**优势**：
- ✅ 不需要修改扩展源码
- ✅ 可以动态切换不同工作模式
- ✅ 支持未来扩展（同步/异步/交互式等多种模式）
- ✅ 充分利用 ai-bot 现有架构

**实现位置**：
```
mcp-servers/page-agent-mcp/src/index.js
└─ 第 166-199 行：systemInstruction 注入
```

## 三、已完成的工作

### 1. 系统提示词注入 ✅

**文件**：`mcp-servers/page-agent-mcp/src/index.js`

**修改内容**：
```javascript
const proxyConfig = {
    baseURL: 'http://localhost:9876/proxy/v1',
    model: 'local',
    apiKey: 'local',
    systemInstruction: `
你是一个智能浏览器助手，具备本地知识库访问能力。

## 知识库使用（最高优先级）

需要信息时，**优先访问本地知识库**：
- 检索：http://localhost:9876/kb/search?q={query}
- 问答：http://localhost:9876/kb/answer?context={context}&question={question}

### 使用场景
- 遇到专业术语 → 先查知识库
- 需要背景信息 → 先查知识库
- 不确定如何操作 → 先查知识库

只有知识库无结果时，才使用外部搜索。

## 工作原则

1. 快速反馈：每步操作及时返回结果
2. 遇到困难：立即报告，不要长时间重试
3. 任务拆分：复杂任务自动分解为小步骤
4. 本地优先：优先使用本地服务（更快、更准确、保护隐私）

## 语言

Default working language: **中文**
按用户使用的语言回复。
`
}
```

### 2. 知识库 API 框架 ✅

**文件**：`niu_api/kb.py`

**端点**：
- `GET /kb/search?q={query}` - 知识检索
- `GET /kb/answer?context={context}&question={question}` - 知识问答
- `GET /kb/health` - 健康检查

**当前状态**：
- ✅ API 框架已创建
- ✅ 返回模拟数据（MBTI、浏览器自动化等）
- ⚠️ 待对接真实知识库（向量检索、RAG系统）

**示例返回**：
```json
{
  "success": true,
  "results": [
    {
      "title": "MBTI人格测试简介",
      "content": "MBTI（Myers-Briggs Type Indicator）是一种人格类型指标...",
      "relevance": 0.95,
      "source": "心理学基础知识"
    }
  ],
  "total": 1
}
```

### 3. 提示词模板定义 ✅

**文件**：`mcp-servers/page-agent-mcp/src/index.js`

**定义了多种工作模式**（已定义但未使用，为未来扩展准备）：
- `knowledge_enhanced` - 知识库增强模式（当前使用）
- `sync` - 同步模式：快速反馈
- `async` - 异步模式：长时间自主工作
- `interactive` - 交互式模式：逐步确认

## 四、测试计划

### 测试 1：知识库 API 访问

```bash
# 启动服务
python -m niu_api

# 测试知识库搜索
curl "http://localhost:9876/kb/search?q=MBTI测试"

# 预期返回
{
  "success": true,
  "results": [
    {
      "title": "MBTI人格测试简介",
      "content": "MBTI（Myers-Briggs Type Indicator）...",
      "relevance": 0.95
    }
  ]
}
```

### 测试 2：Page-Agent 使用知识库

```python
# scripts/test_kb_enhanced_mode.py

from niu_page_agent import execute_task

# 任务：需要知识库支持
result = execute_task("""
完成MBTI人格测试：
1. 打开 https://mbti-test.app/zh-cn/free-personality-test
2. 遇到不确定的问题，先查知识库：http://localhost:9876/kb/search?q=MBTI维度含义
3. 根据知识库信息选择答案
4. 完成整个测试并返回结果
""")

print(result)
```

### 测试 3：验证提示词注入

查看日志：
```bash
tail -f E:/tools/ai-bot/logs/llm_interaction_*.log | grep "知识库使用"
```

应该能看到注入的提示词内容。

## 五、下一步工作

### 优先级 1：对接真实知识库

**当前问题**：知识库 API 返回模拟数据

**解决方案**：
```python
# niu_api/kb.py

@router.get("/search")
async def search_knowledge(q: str, limit: int = 5):
    # TODO: 对接真实向量检索
    # 1. 连接向量数据库
    # 2. 语义搜索
    # 3. 返回相关文档

    # 示例：使用现有的 L0/L1/L2 系统
    from agent.vector_search import search_similar

    results = await search_similar(q, top_k=limit)
    return {
        "success": True,
        "results": results
    }
```

### 优先级 2：实现异步任务系统

**问题**：`execute_task` 是同步阻塞调用

**解决方案**：
```python
# niu_api/async_tasks.py

import asyncio
from datetime import datetime
from typing import Dict

# 任务存储
tasks: Dict[str, dict] = {}

async def execute_task_async(task_id: str, task: str):
    """后台执行任务"""
    tasks[task_id] = {
        "status": "running",
        "started_at": datetime.now(),
        "result": None
    }

    try:
        # 调用 page-agent
        result = await execute_task(task)
        tasks[task_id]["status"] = "completed"
        tasks[task_id]["result"] = result
    except Exception as e:
        tasks[task_id]["status"] = "failed"
        tasks[task_id]["error"] = str(e)

# API 端点
@router.post("/execute_async")
async def execute_async(task: str):
    """提交异步任务"""
    task_id = generate_task_id()

    # 后台执行
    asyncio.create_task(execute_task_async(task_id, task))

    return {"task_id": task_id, "status": "pending"}

@router.get("/task/{task_id}")
async def get_task_result(task_id: str):
    """查询任务结果"""
    if task_id not in tasks:
        raise HTTPException(404, "Task not found")

    return tasks[task_id]
```

### 优先级 3：完善提示词模板

**扩展多种工作模式**：

```javascript
// 在 execute_task 时根据任务类型选择模式
function selectMode(task) {
    if (task.includes('[ASYNC]')) {
        return SYSTEM_PROMPTS.async  // 长时间自主工作
    } else if (task.includes('[SYNC]')) {
        return SYSTEM_PROMPTS.sync  // 快速反馈
    } else {
        return SYSTEM_PROMPTS.knowledge_enhanced  // 默认
    }
}

// 使用示例
execute_task("[ASYNC] 完成MBTI测试并分析结果")  // 异步模式
execute_task("[SYNC] 打开页面返回标题")         // 同步模式
execute_task("完成MBTI测试")                    // 知识库增强模式
```

## 六、架构图

```
┌─────────────────────────────────────────────────┐
│            主 Agent (Python)                     │
│  - 接收用户任务                                   │
│  - 提交给 page-agent                             │
│  - （异步模式）不等待，继续工作                    │
│  - 接收结果，反馈用户                             │
└──────────────────┬──────────────────────────────┘
                   │ HTTP REST API (port 38402)
                   ↓
┌─────────────────────────────────────────────────┐
│         MCP Server (Node.js)                     │
│  - 接收任务 + 注入系统提示词                       │
│  - systemInstruction: 知识库增强模式              │
└──────────────────┬──────────────────────────────┘
                   │ WebSocket (port 38401)
                   ↓
┌─────────────────────────────────────────────────┐
│      Chrome Extension (Page Agent)               │
│  - 执行浏览器操作                                 │
│  - 遇到不懂的内容 → 访问本地知识库                 │
│  - 根据知识库完成任务                             │
└──────────────────┬──────────────────────────────┘
                   │ HTTP (localhost:9876)
                   ↓
┌─────────────────────────────────────────────────┐
│       知识库 API (niu_api/kb.py)                 │
│  - /kb/search?q=xxx 语义搜索                     │
│  - /kb/answer 问答生成                           │
│  - 对接向量数据库、RAG系统                        │
└─────────────────────────────────────────────────┘
```

## 七、关键文件清单

| 文件 | 状态 | 功能 |
|------|------|------|
| `mcp-servers/page-agent-mcp/src/index.js` | ✅ 已修改 | 系统提示词注入 |
| `niu_api/kb.py` | ✅ 已创建 | 知识库 API 框架 |
| `docs/page-agent-system-prompt-injection.md` | ✅ 已创建 | 注入方案文档 |
| `docs/page-agent-original-system-prompt.md` | ✅ 已下载 | 原始提示词参考 |
| `mcp-servers/page-agent-mcp/src/prompts.py` | ✅ 已创建 | 提示词模板（Python，未使用） |

## 八、预期效果

### 场景 1：MBTI 测试（知识库支持）

**用户**：完成MBTI人格测试

**流程**：
1. Page-Agent 打开测试页面
2. 获取第一题："在社交场合中，我的感受是..."
3. 发现需要理解题目 → 访问知识库
4. `GET http://localhost:9876/kb/search?q=MBTI外向内向维度`
5. 收到知识库解释
6. 根据知识选择答案
7. 继续下一题
8. 完成整个测试

**优势**：
- ✅ 不依赖外网搜索（避免限制）
- ✅ 答案基于知识库（更准确）
- ✅ 保护隐私（不泄露到外网）

### 场景 2：复杂研究任务（异步模式）

**用户**：调研浏览器自动化工具，给出技术选型建议

**流程**：
1. 主 Agent 提交任务（不等待）
2. Page-Agent 自主工作（几分钟）：
   - 搜索相关网站
   - 提取信息
   - 整理对比
   - 生成报告
3. 完成后返回结果
4. 主 Agent 反馈给用户

**优势**：
- ✅ 主 Agent 不阻塞
- ✅ Page-Agent 可以深度思考
- ✅ 完整的交付结果

## 九、总结

我们实现了**动态系统提示词注入**，让 Page-Agent：

1. ✅ **具备知识库访问能力**：优先使用本地知识，不依赖外网
2. ✅ **灵活可扩展**：支持多种工作模式，按需注入不同提示词
3. ✅ **不修改源码**：通过 systemInstruction 配置覆盖
4. ✅ **充分复用 ai-bot 架构**：知识库、向量检索、API 服务

**核心价值**：让第四代浏览器 Agent 充分发挥其智能，结合我们的本地知识库，实现真正自主、智能的浏览器自动化！
