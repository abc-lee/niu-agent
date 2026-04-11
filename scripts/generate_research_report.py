#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Page-Agent 开源生态调研报告生成器
"""

import sys

# 设置标准输出编码为 UTF-8
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def generate_report():
    """生成完整调研报告"""
    report = """
# Page-Agent 开源生态调研报告

**调研时间**: 2026-04-11
**调研目标**: 分析 page-agent 及竞品的 MCP 集成和自定义工具注入方案

---

## 一、核心发现

### 1.1 Alibaba Page-Agent 已支持 MCP

**项目地址**: https://github.com/alibaba/page-agent
**Stars**: 16,746 ⭐
**Forks**: 1,358

**关键事件**:
- **Issue #297** (2026-03-18): "MCP (beta) is here" - MCP 功能正式发布
- **PR #283** (2026-03-17): "feat: mcp (WIP)" - MCP 实现代码合并

**MCP 实现分析**:

#### 架构设计
```
┌──────────────┐  stdio   ┌──────────────────┐  WebSocket   ┌──────────────┐
│ Claude /     │◄────────►│ @page-agent/mcp  │◄────────────►│ Hub tab      │
│ Copilot      │  (MCP)   │ (Node.js)        │  (localhost) │ (extension)  │
└──────────────┘          └──────────────────┘              └──────┬───────┘
                                   │                               │
                                   │ HTTP                          │ useAgent
                                   ▼                               ▼
                          ┌──────────────────┐              ┌──────────────┐
                          │ Launcher page    │              │ MultiPage    │
                          │ (localhost:PORT) │              │ Agent        │
                          └──────────────────┘              └──────────────┘
```

#### 暴露的 MCP 工具

| 工具名称 | 输入 | 描述 | 是否支持自定义 |
|---------|------|------|---------------|
| `execute_task` | `{ task: string }` | 执行浏览器任务（自然语言） | ❌ 不支持 |
| `get_status` | — | 返回连接状态 | ❌ |
| `stop_task` | — | 停止当前任务 | ❌ |

#### 关键限制

**❌ 不支持自定义工具注入**

1. **设计理念**: Page-Agent 使用自然语言任务描述，不暴露底层浏览器操作原语
2. **工具粒度**: 只有 3 个工具，没有 click/type/scroll 等细粒度操作
3. **扩展性**: 用户无法注入自己的工具函数或覆盖默认行为

**源代码证据** (`packages/mcp/src/index.js`):
```javascript
mcpServer.registerTool(
  'execute_task',
  {
    description: "Execute a task in user's browser.",
    inputSchema: {
      task: z.string().describe('Task description. Give specific instructions...'),
    },
  },
  async ({ task }) => {
    // 只接受自然语言描述，无法注入自定义逻辑
    const result = await hub.executeTask(task, config)
    return { content: [{ type: 'text', text: result.data }] }
  }
)
```

---

### 1.2 Microsoft Playwright-MCP

**项目地址**: https://github.com/microsoft/playwright-mcp
**Stars**: 30,627 ⭐
**官方支持**: Microsoft 官方项目

**关键特性**:
- 使用 Playwright 的 accessibility tree，不需要视觉模型
- 暴露丰富的浏览器操作工具
- 支持 Node.js 18+

**工具列表** (推测，未完整获取):
- 浏览器导航: `navigate`, `goBack`, `goForward`
- 页面操作: `click`, `type`, `scroll`, `screenshot`
- 等待操作: `waitForSelector`, `waitForNavigation`
- 页面查询: `evaluate`, `query`

**MCP vs CLI 架构选择**:
- **CLI + SKILLS**: 适合编码代理，token 效率更高
- **MCP**: 适合探索性自动化、自愈测试、长期运行的工作流

**自定义工具支持**: ✅ 可能支持（需进一步查看源代码）

---

### 1.3 Browser-Use

**项目地址**: https://github.com/browser-use/browser-use
**Stars**: 87,137 ⭐
**最流行的 AI 浏览器自动化框架**

**关键特性**:
- "Make websites accessible for AI agents"
- 支持多种 LLM（OpenAI、Claude、Gemini 等）
- 提供 Python SDK

**工具调用机制**:
- 基于 LangChain 的工具调用
- 支持自定义工具注册

**自定义工具支持**: ✅ 支持

---

## 二、竞品对比分析

| 项目 | Stars | MCP 支持 | 自定义工具 | 架构复杂度 | 推荐指数 |
|------|-------|---------|-----------|-----------|---------|
| **alibaba/page-agent** | 16,746 | ✅ Beta | ❌ 不支持 | 中等 | ⭐⭐⭐ |
| **microsoft/playwright-mcp** | 30,627 | ✅ 官方 | ✅ 可能支持 | 低 | ⭐⭐⭐⭐⭐ |
| **browser-use** | 87,137 | ❌ | ✅ 支持 | 高 | ⭐⭐⭐⭐⭐ |
| **vercel-labs/agent-browser** | 28,594 | ❌ | ✅ 支持 | 中 | ⭐⭐⭐⭐ |
| **browserbase/stagehand** | 21,999 | ❌ | ✅ 支持 | 中 | ⭐⭐⭐⭐ |

---

## 三、GitHub Issues/PRs 分析

### 3.1 Page-Agent 相关

**Issue #388** (2026-04-02): "[Feature]" - 可能是新功能请求
- 状态: open
- 需要进一步查看详细内容

**Issue #297** (2026-03-18): "MCP (beta) is here" - MCP 发布公告
- 状态: closed
- Chrome Web Store 上架审核中

### 3.2 其他项目 Issues

搜索关键词 "browser automation tool calling" 发现:
- 多个项目在讨论浏览器工具的标准化
- 普遍支持细粒度操作 + 自定义工具

---

## 四、技术方案对比

### 4.1 Page-Agent 的局限性

**优势**:
- ✅ 简单易用，自然语言驱动
- ✅ 已有 MCP 支持（Beta）
- ✅ 浏览器扩展集成良好

**劣势**:
- ❌ 不支持自定义工具注入
- ❌ 只有 3 个粗粒度工具
- ❌ 无法扩展或覆盖默认行为
- ❌ 不适合需要精确控制的场景

### 4.2 Playwright-MCP 的优势

**优势**:
- ✅ Microsoft 官方维护
- ✅ 丰富的浏览器操作工具
- ✅ 基于 accessibility tree，无需视觉模型
- ✅ 社区活跃，文档完善

**劣势**:
- ⚠️ 工具数量多，token 消耗大
- ⚠️ 需要进一步确认自定义工具支持

### 4.3 Browser-Use 的优势

**优势**:
- ✅ 最流行，社区最活跃
- ✅ 支持自定义工具注册
- ✅ 多 LLM 支持
- ✅ Python SDK 易集成

**劣势**:
- ❌ 不支持 MCP 协议
- ❌ 需要单独部署和管理

---

## 五、结论与建议

### 5.1 核心结论

1. **Page-Agent 不适合当前需求**
   - 无法注入自定义工具
   - 架构设计不支持细粒度控制
   - 需要寻找替代方案或进行深度改造

2. **Playwright-MCP 是最佳选择**
   - Microsoft 官方支持，质量有保证
   - 丰富的工具集，适合浏览器自动化
   - 架构简单，易于扩展

3. **Browser-Use 是备选方案**
   - 功能最完善，社区最活跃
   - 支持自定义工具
   - 但不支持 MCP，需要额外适配

### 5.2 技术方案建议

#### 方案一：直接使用 Playwright-MCP（推荐）

**优势**:
- 开箱即用，无需改造
- 官方维护，质量可靠
- 社区活跃，问题易解决

**实施步骤**:
1. 安装 `@playwright/mcp@latest`
2. 配置 MCP 服务器
3. 实现自定义工具注入（如果支持）

#### 方案二：改造 Page-Agent（不推荐）

**挑战**:
- 需要修改核心架构
- 需要从自然语言描述改为工具调用
- 工作量大，风险高

**如果必须改造**:
1. 修改 `execute_task` 工具，支持传入函数名和参数
2. 扩展 MCP 工具列表，暴露底层操作
3. 实现 `register_custom_tool` 机制

#### 方案三：自建 MCP 浏览器服务器（可选）

**适用场景**:
- 需要高度定制化
- 现有方案无法满足需求

**实施步骤**:
1. 使用 Playwright/Puppeteer 作为底层
2. 实现 MCP 协议接口
3. 暴露自定义工具注册 API

---

## 六、后续行动

### 6.1 立即行动

- [ ] 深入分析 Playwright-MCP 源代码，确认自定义工具支持
- [ ] 测试 Playwright-MCP 与现有 MCP 客户端的集成
- [ ] 评估 Browser-Use 的 Python SDK 集成难度

### 6.2 长期规划

- [ ] 跟踪 Page-Agent 的 MCP 功能更新
- [ ] 关注社区是否有自定义工具注入的 PR
- [ ] 建立浏览器自动化最佳实践文档

---

## 七、参考链接

### 核心项目

- [alibaba/page-agent](https://github.com/alibaba/page-agent) - 官方仓库
- [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) - Microsoft 官方 MCP 服务器
- [browser-use/browser-use](https://github.com/browser-use/browser-use) - 最流行的 AI 浏览器自动化框架

### 相关 Issues/PRs

- [Issue #297](https://github.com/alibaba/page-agent/issues/297) - MCP (beta) is here
- [PR #283](https://github.com/alibaba/page-agent/pull/283) - feat: mcp (WIP)

### 其他 MCP 浏览器项目

- [executeautomation/mcp-playwright](https://github.com/executeautomation/mcp-playwright) - 社区版 Playwright MCP
- [ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) - Chrome DevTools MCP
- [merajmehrabi/puppeteer-mcp-server](https://github.com/merajmehrabi/puppeteer-mcp-server) - Puppeteer MCP

---

**报告生成时间**: 2026-04-11 19:15:00
**调研工具**: GitHub API + WebFetch + 代码分析
**调研范围**: GitHub 公开仓库 + Issues + PRs
"""

    print(report)

    # 保存到文件
    output_file = "docs/page-agent-ecosystem-research.md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\n✅ 报告已保存到: {output_file}")


if __name__ == "__main__":
    generate_report()
