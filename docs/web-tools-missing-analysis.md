# Web 工具丢失问题分析报告

> 分析时间：2026-04-10
> 问题：GenericAgent 迁移过程中丢失 web_scan 和 web_execute_js 工具

---

## 一、问题发现

### 症状

用户要求 Agent 打开浏览器时，出现错误：

```
litellm.BadRequestError: AnthropicException - invalid params, tool call result does not follow tool call (2013)
```

### 根因

1. **工具不存在**：`web_execute_js` 和 `web_scan` 在当前项目中未定义
2. **历史消息错误**：AI 尝试调用不存在的工具 → 返回错误 → 历史消息格式错误
3. **API 格式违规**：MiniMax API 要求工具调用后必须紧跟工具结果

---

## 二、原始 GenericAgent 架构

### 文件结构

```
GenericAgent/
├── ga.py                    # 主 Agent 文件
│   ├── web_scan()          # 浏览器扫描函数（第 114 行）
│   ├── web_execute_js()    # 浏览器控制函数（第 165 行）
│   └── GenericAgentHandler # 工具 Handler
│       ├── do_web_scan()         # 工具调度（第 318 行）
│       └── do_web_execute_js()   # 工具调度（第 333 行）
└── TMWebDriver.py           # 浏览器驱动
    └── TMWebDriver         # WebSocket/HTTP 驱动类
```

### 工具功能

**1. web_scan()**

```python
def web_scan(tabs_only=False, switch_tab_id=None, text_only=False):
    """
    获取当前页面的简化HTML内容和标签页列表
    - tabs_only: 仅返回标签页列表
    - switch_tab_id: 切换标签页
    - text_only: 仅返回文本内容
    """
```

**返回格式**：
```json
{
  "status": "success",
  "metadata": {
    "tabs_count": 3,
    "tabs": [...],
    "active_tab": "tab-id-123"
  },
  "content": "<html>...</html>"  // 可选
}
```

**2. web_execute_js()**

```python
def web_execute_js(script, switch_tab_id=None, no_monitor=False):
    """
    执行 JS 脚本控制浏览器
    - script: JavaScript 代码
    - switch_tab_id: 切换标签页
    - no_monitor: 禁用页面变化监控
    """
```

**返回格式**：
```json
{
  "status": "success",
  "js_return": "...",
  "diff": "页面变化摘要",
  "environment": {
    "newTabs": [],
    "reloaded": false
  }
}
```

### 底层架构

**TMWebDriver**：自定义浏览器驱动

```
┌─────────────────┐
│  Python Agent   │
│  (TMWebDriver)  │
└────────┬────────┘
         │ WebSocket (18765)
         │ HTTP (18766)
┌────────▼────────┐
│ 浏览器扩展       │
│ (Chrome/Firefox)│
└─────────────────┘
```

**通信机制**：
- WebSocket：实时双向通信
- HTTP：轮询和命令发送
- 浏览器扩展：注入页面，执行 JS

---

## 三、迁移过程分析

### 当前项目架构

```
ai-bot/
├── agent/
│   ├── handler.py          # 工具 Handler
│   │   ├── file_read()     ✅ 已迁移
│   │   ├── file_write()    ✅ 已迁移
│   │   ├── file_patch()    ✅ 已迁移
│   │   ├── code_run()      ✅ 已迁移
│   │   ├── web_scan()      ❌ 未迁移
│   │   └── web_execute_js() ❌ 未迁移
│   └── runner.py
└── mcp-servers/            # MCP 服务器
```

### 丢失原因

**迁移时间线**：
1. 早期迁移时，主要关注文件操作和代码执行工具
2. Web 工具依赖浏览器扩展和驱动，迁移复杂度高
3. 可能认为 Playwright MCP 更现代化，计划后续添加
4. **最终忘记迁移**

**依赖项**：
- `simple_websocket_server` - WebSocket 服务器
- `bottle` - HTTP 服务器
- `bs4` (BeautifulSoup) - HTML 解析
- 浏览器扩展（Chrome Extension）

---

## 四、解决方案

### 方案 1：完整迁移原始架构（推荐用于兼容性）

**优点**：
- ✅ 完全兼容原始 GenericAgent
- ✅ 浏览器扩展已成熟稳定
- ✅ 支持 Chrome 和 Firefox

**缺点**：
- ⚠️ 需要手动安装浏览器扩展
- ⚠️ 架构较复杂（WebSocket + HTTP）
- ⚠️ 依赖较多

**步骤**：

1. **复制核心文件**：
   ```bash
   cp E:/tools/GenericAgent/TMWebDriver.py E:/tools/ai-bot/agent/
   cp E:/tools/GenericAgent/ga.py E:/tools/ai-bot/agent/web_tools.py
   ```

2. **提取 web 工具函数**：
   - 从 `ga.py` 提取 `web_scan()` 和 `web_execute_js()`
   - 提取相关辅助函数：`get_html()`, `execute_js_rich()`, `first_init_driver()`

3. **添加 Handler 方法**：
   - 在 `agent/handler.py` 添加 `do_web_scan()` 和 `do_web_execute_js()`

4. **安装依赖**：
   ```bash
   pip install simple-websocket-server bottle beautifulsoup4
   ```

5. **配置浏览器扩展**：
   - 打包扩展
   - 提供安装说明

**预计工作量**：4-6 小时

---

### 方案 2：使用 Playwright MCP 服务器（推荐用于现代化）

**优点**：
- ✅ 现代化架构，API 更简洁
- ✅ 内置浏览器自动化（无需扩展）
- ✅ 支持多浏览器（Chromium, Firefox, WebKit）
- ✅ 更好的错误处理和稳定性

**缺点**：
- ⚠️ 需要编写新的工具实现
- ⚠️ API 与原始工具不同，需要适配

**步骤**：

1. **创建 MCP 服务器**：
   ```bash
   mkdir -p mcp-servers/browser-server/src/niu_browser_server
   ```

2. **定义工具 Schema**：
   ```python
   TOOL_SCHEMAS = {
       "browser_navigate": {
           "description": "Navigate to URL",
           "inputSchema": {
               "type": "object",
               "properties": {
                   "url": {"type": "string"}
               }
           }
       },
       "browser_scan": {
           "description": "Get page content",
           "inputSchema": {
               "type": "object",
               "properties": {
                   "text_only": {"type": "boolean"}
               }
           }
       },
       "browser_execute_js": {
           "description": "Execute JavaScript",
           "inputSchema": {
               "type": "object",
               "properties": {
                   "script": {"type": "string"}
               }
           }
       }
   }
   ```

3. **实现工具函数**：
   ```python
   from playwright.sync_api import sync_playwright

   def browser_navigate(url: str):
       with sync_playwright() as p:
           browser = p.chromium.launch()
           page = browser.new_page()
           page.goto(url)
           return {"status": "success"}

   def browser_scan(text_only: bool = False):
       # 实现页面扫描
       pass

   def browser_execute_js(script: str):
       # 实现 JS 执行
       pass
   ```

4. **注册到主 Agent**：
   - 在 `config/agents/niu.md` 添加 `browser-server`

**预计工作量**：8-12 小时

---

### 方案 3：临时禁用 Web 工具（最快解决）

**适用场景**：
- 项目暂时不需要浏览器控制
- 快速解决当前 API 错误

**步骤**：

1. **清空历史消息**：
   ```
   用户输入：/new
   ```

2. **更新提示词**：
   - 在 `config/agents/niu.md` 移除浏览器相关能力说明
   - 避免用户误以为有此功能

**工作量**：10 分钟

---

## 五、推荐方案

### 短期（立即解决）

**使用方案 3**：
1. 输入 `/new` 清空历史错误消息
2. 更新 Agent 提示词，移除浏览器能力说明
3. 避免用户尝试使用不存在的功能

### 中期（完整迁移）

**选择方案 1 或方案 2**：

**选择依据**：
- **选方案 1**：如果需要与原始 GenericAgent 完全兼容
- **选方案 2**：如果希望更现代化的架构，且不介意 API 变化

**个人建议**：方案 2（Playwright）
- 更现代化
- 无需浏览器扩展
- 更好的维护性

---

## 六、工具 Schema 对比

### 原始 GenericAgent

```json
{
  "web_scan": {
    "description": "获取当前页面的简化HTML内容和标签页列表",
    "parameters": {
      "type": "object",
      "properties": {
        "tabs_only": {"type": "boolean", "default": false},
        "switch_tab_id": {"type": "string"},
        "text_only": {"type": "boolean", "default": false}
      }
    }
  },
  "web_execute_js": {
    "description": "执行 JS 脚本来控制浏览器",
    "parameters": {
      "type": "object",
      "properties": {
        "script": {"type": "string"},
        "switch_tab_id": {"type": "string"},
        "no_monitor": {"type": "boolean", "default": false},
        "save_to_file": {"type": "string"}
      },
      "required": ["script"]
    }
  }
}
```

### Playwright MCP（建议）

```json
{
  "browser_navigate": {
    "description": "Navigate to URL",
    "parameters": {
      "type": "object",
      "properties": {
        "url": {"type": "string"}
      },
      "required": ["url"]
    }
  },
  "browser_scan": {
    "description": "Get page content and tab list",
    "parameters": {
      "type": "object",
      "properties": {
        "text_only": {"type": "boolean", "default": false},
        "tabs_only": {"type": "boolean", "default": false}
      }
    }
  },
  "browser_execute_js": {
    "description": "Execute JavaScript on current page",
    "parameters": {
      "type": "object",
      "properties": {
        "script": {"type": "string"},
        "save_to_file": {"type": "string"}
      },
      "required": ["script"]
    }
  }
}
```

---

## 七、后续行动

### 立即行动

- [ ] 用户输入 `/new` 清空历史
- [ ] 确认 API 错误消失

### 短期（本周）

- [ ] 决定使用方案 1 还是方案 2
- [ ] 创建实施计划文档
- [ ] 开始迁移工作

### 中期（下周）

- [ ] 完成工具迁移
- [ ] 编写测试脚本
- [ ] 更新用户文档

---

## 八、风险评估

### 方案 1 风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 浏览器扩展兼容性 | 中 | 测试主流浏览器版本 |
| WebSocket 稳定性 | 低 | 添加重连机制 |
| 依赖冲突 | 低 | 隔离虚拟环境 |

### 方案 2 风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| API 不兼容 | 高 | 编写适配层 |
| Playwright 学习曲线 | 中 | 参考官方文档 |
| 性能开销 | 低 | 测试并优化 |

---

**分析完成时间**：2026-04-10
**问题严重程度**：HIGH
**建议优先级**：URGENT（立即清空历史）+ HIGH（完整迁移）
