# 浏览器自动化技术调研报告

> 调研时间：2026-04-10
> 目标：选择最适合项目的浏览器自动化技术方案

---

## 一、技术演进脉络

### 1. 第一代：脚本驱动（2010s）

**代表技术**：Selenium, Puppeteer, Playwright

**特点**：
- 需要编写详细的自动化脚本
- 基于DOM选择器和坐标定位
- 维护成本高，页面变化易导致脚本失效
- 无智能决策能力

**局限性**：
- ⚠️ 无法应对动态页面变化
- ⚠️ 需要人工编写每个操作步骤
- ⚠️ 错误处理能力弱

### 2. 第二代：AI增强（2023-2025）

**代表技术**：Browser Use, Playwright + LLM

**特点**：
- 自然语言驱动
- LLM理解页面语义
- 支持复杂任务规划
- 自动错误恢复

**局限性**：
- ⚠️ 依赖后端浏览器驱动（Playwright）
- ⚠️ 需要启动独立浏览器进程
- ⚠️ 性能开销较大（进程通信、截图识别）

### 3. 第三代：原生智能（2026）

**代表技术**：Page-Agent（阿里），AgentCore Browser（AWS）

**特点**：
- **纯前端运行**：AI直接"寄生"在网页内
- **零后端依赖**：无需浏览器驱动、无需扩展
- **极低延迟**：直接操作DOM，无进程通信
- **Token高效**：DOM解析替代截图识别

---

## 二、主流开源技术对比

### 1. Page-Agent（阿里巴巴，2026年3月）

**GitHub Stars**：9,000+（发布即爆火）

**核心架构**：
```
┌─────────────────────────────────────┐
│  用户网页（前端）                    │
│  ┌───────────────────────────────┐  │
│  │ Page-Agent (一行JS引入)       │  │
│  │  - DOM解析                    │  │
│  │  - LLM决策                    │  │
│  │  - 自动执行                   │  │
│  └───────────────────────────────┘  │
│  直接操作页面元素                    │
└─────────────────────────────────────┘
```

**技术特点**：
- ✅ **纯前端运行**：一行 `<script src="page-agent.js"></script>` 即可
- ✅ **无后端依赖**：无需浏览器驱动、无需WebSocket/HTTP通信
- ✅ **极低延迟**：DOM操作 + LLM决策，无进程通信开销
- ✅ **Token高效**：DOM解析 < 截图识别（10x+ Token节省）
- ✅ **多标签页支持**：每个标签页独立Agent实例
- ✅ **自然语言控制**：用户直接用中文描述任务

**使用示例**：
```html
<!-- 网站引入Page-Agent -->
<script src="https://cdn.example.com/page-agent.js"></script>
<script>
// 一行代码激活AI能力
PageAgent.enable('你的任务描述');
</script>
```

**Agent控制示例**：
```javascript
// 用户：帮我填写这个表单
PageAgent.run('填写注册表单，姓名填张三，邮箱填test@example.com');

// Agent自动执行：
// 1. 解析DOM找到表单元素
// 2. 定位姓名、邮箱输入框
// 3. 自动填写并提交
```

**技术原理**：
1. **DOM解析**：
   ```javascript
   // 提取页面结构（非截图）
   const pageStructure = PageAgent.parseDOM();
   // 转换为简洁的JSON格式
   // {inputs: [...], buttons: [...], links: [...]}
   ```

2. **LLM决策**：
   ```javascript
   // 发送DOM结构到LLM（Token少）
   const action = await llm.decide(pageStructure, userTask);
   // 返回：{action: 'click', selector: '#submit-btn'}
   ```

3. **自动执行**：
   ```javascript
   // 直接操作DOM
   document.querySelector(action.selector).click();
   ```

**对比传统方案**：
| 维度 | Page-Agent | Browser Use | 传统Playwright |
|------|-----------|-------------|---------------|
| 后端依赖 | ❌ 无 | ✅ Playwright进程 | ✅ 浏览器驱动 |
| 浏览器扩展 | ❌ 无 | ❌ 无 | ❌ 无 |
| 延迟 | 极低（DOM直接操作） | 中（进程通信） | 高（多次通信） |
| Token消耗 | 低（DOM解析） | 高（截图识别） | N/A |
| 自然语言 | ✅ | ✅ | ❌ |
| 跨域限制 | ⚠️ 受浏览器限制 | ✅ 无限制 | ✅ 无限制 |

**限制**：
- ⚠️ **跨域限制**：只能在引入Page-Agent的页面内操作
- ⚠️ **无法操作系统级UI**：如文件上传对话框、浏览器菜单等

**最佳场景**：
- 用户需要在自己网站上添加AI助手
- 自动化测试（前端E2E测试）
- 网页爬虫（单页面内数据提取）
- 表单自动填写

---

### 2. Browser Use（开源，2025年）

**GitHub Stars**：持续增长中

**核心架构**：
```
┌──────────────────┐
│  Python Agent    │
│  (Browser Use)   │
└────────┬─────────┘
         │ LangChain API
         │ Playwright CDP
┌────────▼─────────┐
│  浏览器进程      │
│  (Chromium)      │
└──────────────────┘
```

**技术特点**：
- ✅ **AI驱动**：集成LangChain，支持GPT-4、Claude等LLM
- ✅ **自然语言控制**：用户用英语描述任务
- ✅ **多标签页管理**：自动切换标签页
- ✅ **视觉识别**：结合DOM和视觉理解
- ✅ **自我纠正**：遇到错误自动重试
- ✅ **跨平台支持**：支持Chromium、Firefox、WebKit

**使用示例**：
```python
from browser_use import Agent
from langchain_openai import ChatOpenAI

# 初始化Agent
agent = Agent(
    task="打开淘宝，搜索'Python书籍'，找出价格最低的三本书",
    llm=ChatOpenAI(model="gpt-4"),
)

# 执行任务
result = await agent.run()
```

**工作流程**：
1. Agent接收自然语言任务
2. LLM规划执行步骤
3. 通过Playwright控制浏览器
4. 实时解析页面DOM + 截图识别
5. 执行操作（点击、输入、滚动等）
6. 返回结果

**对比Page-Agent**：
| 维度 | Browser Use | Page-Agent |
|------|-------------|-----------|
| 后端依赖 | ✅ Playwright进程 | ❌ 无 |
| 跨域能力 | ✅ 可跨域操作 | ⚠️ 受限 |
| 系统级操作 | ✅ 支持文件上传等 | ❌ 不支持 |
| 延迟 | 中（进程通信） | 极低（DOM直接） |
| Token消耗 | 高（截图识别） | 低（DOM解析） |
| 部署复杂度 | 中（需安装依赖） | 极低（一行代码） |

**最佳场景**：
- 复杂的跨页面自动化任务
- 需要操作系统级交互（文件上传、下载）
- 网页爬虫（跨域数据采集）
- 自动化测试（多页面流程）

---

### 3. Amazon Bedrock AgentCore Browser Tool（AWS，2025年8月）

**定位**：全托管的云上浏览器工具

**核心架构**：
```
┌──────────────────┐
│  用户Agent       │
│  (AWS Lambda)    │
└────────┬─────────┘
         │ AWS SDK
         │
┌────────▼─────────┐
│  AgentCore       │
│  Browser Tool    │  ← 全托管服务
│  (云端浏览器)    │
└────────┬─────────┘
         │ CDP + OS级扩展
┌────────▼─────────┐
│  云端浏览器实例  │
│  (Chromium)      │
└──────────────────┘
```

**技术特点**：
- ✅ **全托管服务**：无需维护浏览器基础设施
- ✅ **OS级交互**：支持鼠标拖动、系统对话框（CDP搞不定的操作）
- ✅ **企业级安全**：AWS安全体系，适合生产环境
- ✅ **自动扩展**：云原生架构，自动处理并发
- ✅ **集成AWS生态**：与Lambda、S3、DynamoDB等无缝集成

**OS级交互能力**（2026年4月新增）：
```python
import boto3

bedrock = boto3.client('bedrock-agent-runtime')

# 处理系统对话框
response = bedrock.invoke_browser_tool(
    toolId='browser-tool-1',
    action={
        'type': 'handle_system_dialog',
        'dialog_type': 'file_upload',
        'files': ['/path/to/file.pdf']
    }
)

# 鼠标拖动操作
response = bedrock.invoke_browser_tool(
    toolId='browser-tool-1',
    action={
        'type': 'mouse_drag',
        'from': {'x': 100, 'y': 200},
        'to': {'x': 400, 'y': 300}
    }
)
```

**对比开源方案**：
| 维度 | AgentCore Browser | Browser Use | Page-Agent |
|------|-------------------|-------------|-----------|
| 托管模式 | ✅ 全托管（云） | ❌ 自托管 | ❌ 自托管 |
| OS级操作 | ✅ 支持 | ⚠️ 有限 | ❌ 不支持 |
| 企业级安全 | ✅ AWS安全体系 | ⚠️ 需自行保障 | ⚠️ 前端安全限制 |
| 成本 | ⚠️ 按使用计费 | ✅ 免费开源 | ✅ 免费开源 |
| 可控性 | ⚠️ 依赖AWS | ✅ 完全控制 | ✅ 完全控制 |

**最佳场景**：
- 企业级应用，需要云托管
- 需要OS级交互（文件上传、系统对话框）
- 高并发场景（AWS自动扩展）
- AWS生态系统内的项目

---

## 三、技术选型建议

### 场景1：个人项目、本地助手（本项目）

**推荐**：**Browser Use**（短期） + **Page-Agent**（长期）

**理由**：
1. **短期**：
   - Browser Use成熟稳定，支持复杂跨页面任务
   - 部署简单，Python生态友好
   - 自然语言驱动，符合Agent设计理念

2. **长期**：
   - Page-Agent极低延迟，Token高效
   - 无后端依赖，架构简洁
   - 阿里开源，中文文档完善

**实施路线**：
```
第1阶段（1周）：
- 集成Browser Use作为MCP服务器
- 实现基础浏览器自动化（打开页面、点击、输入）
- 测试稳定性

第2阶段（1个月）：
- 评估Page-Agent成熟度
- 如果稳定，逐步迁移到Page-Agent
- 保留Browser Use作为备选（复杂任务）
```

---

### 场景2：企业级生产应用

**推荐**：**Amazon Bedrock AgentCore Browser Tool**

**理由**：
- 全托管，无需维护浏览器基础设施
- 企业级安全和合规
- OS级交互能力
- AWS生态集成

**注意**：
- ⚠️ 成本较高（按使用计费）
- ⚠️ 依赖AWS服务

---

### 场景3：网站AI助手（对外服务）

**推荐**：**Page-Agent**

**理由**：
- 一行代码集成，用户体验极佳
- 无后端依赖，降低运维成本
- Token高效，降低运营成本

**限制**：
- ⚠️ 仅限单页面操作
- ⚠️ 无法跨域（受浏览器安全策略限制）

---

## 四、本项目具体实施计划

### 方案：混合架构（Browser Use + Page-Agent）

#### 架构设计

```
┌─────────────────────────────────────────┐
│  主Agent (GenericAgent)                 │
│  - 自然语言理解                          │
│  - 任务规划                              │
└────────┬────────────────────────────────┘
         │
         ├─► 简单任务（单页面）
         │   └─► Page-Agent MCP服务器
         │       - 极低延迟
         │       - Token高效
         │
         └─► 复杂任务（跨页面/系统交互）
             └─► Browser Use MCP服务器
                 - 跨域能力
                 - OS级交互
```

#### 工具Schema设计

**1. browser_scan（Page-Agent实现）**

```json
{
  "name": "browser_scan",
  "description": "扫描当前页面内容（极低延迟，适用于单页面）",
  "inputSchema": {
    "type": "object",
    "properties": {
      "text_only": {
        "type": "boolean",
        "default": false,
        "description": "仅返回文本内容"
      }
    }
  }
}
```

**返回格式**：
```json
{
  "status": "success",
  "content": "页面DOM解析结果（简洁JSON格式）",
  "elements": {
    "inputs": [...],
    "buttons": [...],
    "links": [...]
  }
}
```

**2. browser_execute_js（Page-Agent实现）**

```json
{
  "name": "browser_execute_js",
  "description": "在当前页面执行JavaScript（DOM直接操作）",
  "inputSchema": {
    "type": "object",
    "properties": {
      "script": {
        "type": "string",
        "description": "JavaScript代码"
      }
    },
    "required": ["script"]
  }
}
```

**3. browser_navigate（Browser Use实现）**

```json
{
  "name": "browser_navigate",
  "description": "打开网页（支持跨页面任务）",
  "inputSchema": {
    "type": "object",
    "properties": {
      "url": {
        "type": "string",
        "description": "目标URL"
      }
    },
    "required": ["url"]
  }
}
```

**4. browser_task（Browser Use实现）**

```json
{
  "name": "browser_task",
  "description": "执行复杂的浏览器任务（自然语言描述）",
  "inputSchema": {
    "type": "object",
    "properties": {
      "task": {
        "type": "string",
        "description": "任务描述，例如：打开淘宝搜索Python书籍并找出价格最低的"
      }
    },
    "required": ["task"]
  }
}
```

#### 实施步骤

**第1步：Browser Use MCP服务器（1周）**

1. 创建MCP服务器：
   ```bash
   mkdir -p mcp-servers/browser-use-server/src/niu_browser_use_server
   ```

2. 安装依赖：
   ```bash
   pip install browser-use playwright
   playwright install chromium
   ```

3. 实现工具：
   ```python
   # mcp-servers/browser-use-server/src/niu_browser_use_server/__init__.py

   from browser_use import Agent
   from langchain_openai import ChatOpenAI

   TOOL_SCHEMAS = {
       "browser_navigate": {
           "description": "打开网页",
           "inputSchema": {
               "type": "object",
               "properties": {
                   "url": {"type": "string"}
               },
               "required": ["url"]
           }
       },
       "browser_task": {
           "description": "执行复杂浏览器任务（自然语言）",
           "inputSchema": {
               "type": "object",
               "properties": {
                   "task": {"type": "string"}
               },
               "required": ["task"]
           }
       }
   }

   def get_tool_schemas():
       return [
           {"name": name, **schema}
           for name, schema in TOOL_SCHEMAS.items()
       ]

   async def browser_navigate(url: str):
       """打开网页"""
       from playwright.async_api import async_playwright

       async with async_playwright() as p:
           browser = await p.chromium.launch(headless=False)
           page = await browser.new_page()
           await page.goto(url)

           # 返回页面信息
           title = await page.title()
           return {
               "status": "success",
               "title": title,
               "url": page.url
           }

   async def browser_task(task: str):
       """执行复杂任务"""
       agent = Agent(
           task=task,
           llm=ChatOpenAI(model="gpt-4"),
       )

       result = await agent.run()
       return {
           "status": "success",
           "result": result
       }
   ```

4. 配置到主Agent：
   ```yaml
   # config/mcp-servers.yaml
   browser-use-server:
     command: ${PYTHON_PATH}
     args:
       - "-m"
       - "niu_browser_use_server"
     workdir: ../mcp-servers/browser-use-server/src
     preload: true
   ```

**第2步：Page-Agent集成（2周）**

1. 研究Page-Agent API：
   ```bash
   git clone https://github.com/alibaba/page-agent.git
   ```

2. 创建MCP服务器：
   ```python
   # mcp-servers/page-agent-server/src/niu_page_agent_server/__init__.py

   TOOL_SCHEMAS = {
       "browser_scan": {
           "description": "扫描当前页面（极低延迟）",
           "inputSchema": {
               "type": "object",
               "properties": {
                   "text_only": {"type": "boolean", "default": false}
               }
           }
       },
       "browser_execute_js": {
           "description": "执行JavaScript",
           "inputSchema": {
               "type": "object",
               "properties": {
                   "script": {"type": "string"}
               },
               "required": ["script"]
           }
       }
   }

   def browser_scan(text_only: bool = False):
       """扫描页面（调用Page-Agent）"""
       # 这里需要研究Page-Agent的Python SDK
       # 如果Page-Agent仅支持前端JS，可能需要：
       # 1. 在Electron中注入Page-Agent脚本
       # 2. 通过IPC调用
       pass

   def browser_execute_js(script: str):
       """执行JS"""
       pass
   ```

3. 集成策略：
   - **方案A**：Page-Agent提供Python SDK → 直接调用
   - **方案B**：Page-Agent仅前端JS → 通过Electron注入并IPC通信

**第3步：智能调度（1周）**

1. 实现路由逻辑：
   ```python
   # agent/handler.py

   def dispatch_browser_tool(self, tool_name, params):
       """智能路由到最适合的工具"""

       if tool_name in ["browser_scan", "browser_execute_js"]:
           # 单页面操作 → Page-Agent
           return self.call_tool("page-agent-server/" + tool_name, params)

       elif tool_name in ["browser_navigate", "browser_task"]:
           # 跨页面任务 → Browser Use
           return self.call_tool("browser-use-server/" + tool_name, params)
   ```

2. 添加上下文感知：
   ```python
   # 根据任务复杂度自动选择
   def auto_select_browser_tool(self, task_description):
       """根据任务描述自动选择工具"""

       if self._is_simple_task(task_description):
           return "page-agent-server/browser_scan"
       else:
           return "browser-use-server/browser_task"
   ```

---

## 五、风险评估

### 技术风险

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| Page-Agent API不稳定 | 高 | 中 | 保留Browser Use作为备选 |
| Browser Use性能瓶颈 | 中 | 低 | 优化任务规划，减少不必要的操作 |
| 跨域问题 | 高 | 中 | Browser Use处理跨域任务 |
| Token消耗过大 | 中 | 中 | 优先使用Page-Agent（Token高效） |

### 项目风险

| 风险 | 影响 | 概率 | 缓解措施 |
|------|------|------|----------|
| 学习曲线陡峭 | 低 | 中 | 两个工具都有完善文档 |
| 维护成本高 | 中 | 低 | 混合架构，灵活切换 |

---

## 六、成本对比

### 开发成本

| 方案 | 预计工作量 | 学习成本 |
|------|-----------|----------|
| Browser Use单独 | 1周 | 低 |
| Page-Agent单独 | 2周 | 中 |
| 混合架构 | 4周 | 中 |

### 运行成本

| 方案 | Token消耗 | 服务器开销 |
|------|----------|-----------|
| Page-Agent | 低 | 极低（无后端） |
| Browser Use | 高（截图识别） | 中（浏览器进程） |
| 混合架构 | 中（智能选择） | 中 |

---

## 七、最终推荐

### 短期方案（1个月内）

**使用Browser Use单独方案**

**理由**：
1. 技术成熟，文档完善
2. 开发周期短（1周）
3. 支持复杂跨页面任务
4. Python生态友好

**实施步骤**：
1. 创建`browser-use-server` MCP服务器
2. 实现4个核心工具
3. 配置到主Agent
4. 测试并上线

### 中期方案（3个月内）

**迁移到混合架构（Page-Agent + Browser Use）**

**理由**：
1. Page-Agent成熟度提升
2. Token消耗更低
3. 延迟更低
4. 架构更简洁

**迁移策略**：
1. 先保留Browser Use
2. 逐步引入Page-Agent
3. 智能路由自动选择
4. 最终Browser Use仅处理复杂任务

### 长期方案（6个月后）

**评估AgentCore Browser（如果项目商业化）**

**条件**：
- 用户量增长，需要云托管
- 需要企业级安全
- 预算充足

---

## 八、参考资源

### Page-Agent
- GitHub: https://github.com/alibaba/page-agent
- 文档: https://page-agent.dev（待确认）
- 论文: 《Page-Agent: In-Browser GUI Agent with DOM Understanding》

### Browser Use
- GitHub: https://github.com/browser-use/browser-use
- 官网: https://browser-use.com
- 文档: https://docs.browser-use.com

### Amazon Bedrock AgentCore Browser
- AWS文档: https://docs.aws.amazon.com/bedrock/
- 博客: https://aws.amazon.com/blogs/

---

**报告完成时间**：2026-04-10
**下次评估时间**：2026-07-10（3个月后）
