# Page-Agent 开源生态调研 - 快速总结

**调研时间**: 2026-04-11
**核心结论**: Page-Agent 不支持自定义工具注入，推荐使用 Playwright-MCP

---

## 一、核心发现

### 1. Page-Agent 已支持 MCP (Beta)

- ✅ 官方支持 MCP 协议
- ✅ 发布了 `@page-agent/mcp` NPM 包
- ❌ **不支持自定义工具注入**

### 2. 架构限制

**Page-Agent 的设计理念**:
- 自然语言驱动，不暴露底层浏览器操作原语
- 只有 3 个粗粒度工具: `execute_task`, `get_status`, `stop_task`
- 工具在编译时固定，无法运行时注册

**源代码证据**:
```javascript
// packages/mcp/src/index.js
mcpServer.registerTool('execute_task', {
  description: "Execute a task in user's browser.",
  inputSchema: {
    task: z.string().describe('Task description...'),  // 只接受字符串
  },
}, async ({ task }) => {
  // 无法传入自定义工具或回调函数
  const result = await hub.executeTask(task, config)
  return { content: [{ type: 'text', text: result.data }] }
})
```

---

## 二、竞品对比

| 项目 | Stars | MCP 支持 | 自定义工具 | 推荐指数 |
|------|-------|---------|-----------|---------|
| **alibaba/page-agent** | 16,746 | ✅ Beta | ❌ 不支持 | ⭐⭐⭐ |
| **microsoft/playwright-mcp** | 30,627 | ✅ 官方 | ✅ 可能支持 | ⭐⭐⭐⭐⭐ |
| **browser-use** | 87,137 | ❌ | ✅ 支持 | ⭐⭐⭐⭐⭐ |

---

## 三、推荐方案

### 方案一: 使用 Playwright-MCP (强烈推荐)

**优势**:
- ✅ Microsoft 官方维护
- ✅ 丰富的浏览器操作工具
- ✅ 基于 accessibility tree，无需视觉模型
- ✅ 社区活跃，文档完善

**安装**:
```bash
npm install @playwright/mcp@latest
```

**配置** (Claude Desktop):
```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["@playwright/mcp@latest"]
    }
  }
}
```

---

### 方案二: 使用 Browser-Use

**优势**:
- ✅ 最流行的 AI 浏览器自动化框架 (87,137 stars)
- ✅ 支持自定义工具注册
- ✅ 多 LLM 支持

**劣势**:
- ❌ 不支持 MCP 协议
- ❌ 需要单独部署和管理

---

### 方案三: 改造 Page-Agent (不推荐)

**改造方案**:
1. 修改 `execute_task` 工具，支持传入函数名和参数
2. 扩展 MCP 工具列表，暴露底层操作
3. 实现 `register_custom_tool` 机制

**挑战**:
- 需要修改核心架构
- 需要从自然语言描述改为工具调用
- 工作量大，风险高

---

## 四、后续行动

### 立即行动

- [x] 调研 Page-Agent 开源生态
- [x] 分析 MCP 实现和自定义工具支持
- [x] 生成完整调研报告
- [ ] 测试 Playwright-MCP 与现有系统集成
- [ ] 评估 Browser-Use 的 Python SDK 集成

### 长期规划

- [ ] 跟踪 Page-Agent 的 MCP 功能更新
- [ ] 关注社区是否有自定义工具注入的 PR
- [ ] 建立浏览器自动化最佳实践文档

---

## 五、详细文档

- 📊 [完整调研报告](./page-agent-ecosystem-research.md)
- 💻 [源代码分析](./page-agent-mcp-source-analysis.md)

---

## 六、参考链接

### 核心项目

- [alibaba/page-agent](https://github.com/alibaba/page-agent) - 官方仓库
- [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) - Microsoft 官方 MCP 服务器
- [browser-use/browser-use](https://github.com/browser-use/browser-use) - 最流行的 AI 浏览器自动化框架

### 关键 Issues/PRs

- [Issue #297](https://github.com/alibaba/page-agent/issues/297) - MCP (beta) is here
- [PR #283](https://github.com/alibaba/page-agent/pull/283) - feat: mcp (WIP)

---

**总结**: Page-Agent 不适合需要自定义工具注入的场景，推荐直接使用 Playwright-MCP 或 Browser-Use。
