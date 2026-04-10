# Page-Agent 测试执行指南

> 创建时间：2026-04-10
> 预计测试时间：30 分钟

---

## 🚀 快速开始（方案 B - 纯前端测试）

### 第一步：打开测试页面

**方法 1：直接打开文件**
```bash
# Windows
start E:\tools\ai-bot\scripts\test-page-agent.html

# 或者用浏览器直接打开
chrome E:\tools\ai-bot\scripts\test-page-agent.html
```

**方法 2：启动本地服务器**
```bash
cd E:\tools\ai-bot\scripts
python -m http.server 8080

# 然后浏览器访问
# http://localhost:8080/test-page-agent.html
```

### 第二步：执行测试

页面打开后，你会看到 4 个测试按钮：

1. **测试 1：点击按钮** - 测试基础点击功能
2. **测试 2：填写表单** - 测试表单自动填写
3. **测试 3：提取信息** - 测试页面信息提取
4. **测试 4：复杂任务** - 测试多步骤任务

**操作步骤**：
1. 点击每个测试按钮
2. 观察页面自动操作
3. 查看日志输出
4. 记录统计数据

### 第三步：记录结果

**需要记录的数据**：

| 测试项 | 是否成功 | 耗时（ms） | 错误信息 |
|--------|---------|-----------|---------|
| 测试 1 | ✅/❌ | ___ms | |
| 测试 2 | ✅/❌ | ___ms | |
| 测试 3 | ✅/❌ | ___ms | |
| 测试 4 | ✅/❌ | ___ms | |

**观察指标**：
- ✅ 成功率
- ⏱️ 平均延迟
- 💾 内存占用（Chrome DevTools → Performance）
- 🎯 准确性

### 第四步：填写测试报告

测试完成后，填写以下信息：

```markdown
## 测试结果

### 环境信息
- 操作系统：Windows 11 Pro
- Chrome 版本：___
- Page-Agent 版本：1.7.1
- 测试日期：2026-04-10

### 功能测试结果

#### 测试 1：点击按钮
- 结果：✅ 成功 / ❌ 失败
- 耗时：___ms
- 问题：___（如有）

#### 测试 2：填写表单
- 结果：✅ 成功 / ❌ 失败
- 耗时：___ms
- 准确性：✅ 完全正确 / ⚠️ 部分正确 / ❌ 错误
- 问题：___（如有）

#### 测试 3：提取信息
- 结果：✅ 成功 / ❌ 失败
- 耗时：___ms
- 准确性：✅ 完全正确 / ⚠️ 部分正确 / ❌ 错误
- 问题：___（如有）

#### 测试 4：复杂任务
- 结果：✅ 成功 / ❌ 失败
- 耗时：___ms
- 问题：___（如有）

### 性能数据
- 成功率：___%
- 平均延迟：___ms
- 内存占用：___MB
- Token 消耗：___（如果有统计）

### 发现的问题
1. ___
2. ___
3. ___

### 总体评价
- 优点：___
- 缺点：___
- 建议：___
```

---

## 🔧 方案 A 测试（MCP Server）

### 前置条件

**1. 安装 Chrome 扩展**

访问以下链接安装：
```
https://chromewebstore.google.com/detail/page-agent-ext/akldabonmimlicnjlflnapfeklbfemhj
```

**注意**：
- 需要科学上网（Chrome 扩展商店）
- 安装后浏览器右上角会出现扩展图标

**2. 申请通义千问 API Key**

访问：
```
https://dashscope.aliyuncs.com/
```

步骤：
1. 注册阿里云账号
2. 开通 DashScope 服务
3. 获取 API Key（新用户有 100万 Tokens 免费额度）

**3. 配置 MCP Server**

编辑文件：`config/mcp-servers.yaml`

添加：
```yaml
page-agent-mcp:
  command: npx
  args:
    - "-y"
    - "@page-agent/mcp"
  env:
    LLM_BASE_URL: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_API_KEY: "你的-API-KEY"  # 替换为实际的 API Key
    LLM_MODEL_NAME: "qwen3.5-plus"
    PORT: "38401"
```

### 测试步骤

**步骤 1：启动 MCP Server**

```bash
# 在项目根目录
npx -y @page-agent/mcp
```

**预期现象**：
- 终端显示 "MCP Server started on port 38401"
- 浏览器自动打开 launcher page
- 扩展自动打开 Hub tab

**步骤 2：验证连接**

在 Agent 中测试：

```python
from agent.tool_registry import get_registry

registry = get_registry()
status = registry.get("page-agent-mcp/get_status")()
print(status)  # 应该返回 {"connected": true, "busy": false}
```

**步骤 3：执行任务**

```python
# 简单任务
result = registry.get("page-agent-mcp/execute_task")(
    task="打开百度，搜索'Python教程'"
)

# 复杂任务
result = registry.get("page-agent-mcp/execute_task")(
    task="打开淘宝，搜索'Python书籍'，找出价格最低的三本书"
)
```

**步骤 4：记录结果**

与方案 B 相同的表格格式。

---

## 📊 性能测试方法

### 延迟测试

**Chrome DevTools**：
1. 打开 DevTools (F12)
2. 切换到 "Performance" 标签
3. 点击录制按钮
4. 执行测试
5. 停止录制
6. 分析时间线

**测量指标**：
- 脚本执行时间
- DOM 操作时间
- 网络请求时间（如果有）

### 内存测试

**Chrome DevTools**：
1. 打开 DevTools (F12)
2. 切换到 "Memory" 标签
3. 点击 "Take heap snapshot"
4. 执行测试前后的内存对比

### Token 消耗测试

**方法**：
- 查看 API 调用日志
- 对比 Page-Agent vs Browser Use

---

## ❓ 常见问题

### Q1：测试页面无法加载 Page-Agent？

**可能原因**：
- CDN 访问受限

**解决方案**：
- 使用国内镜像源（已配置）
- 或下载到本地

### Q2：MCP Server 启动失败？

**可能原因**：
- Chrome 扩展未安装
- 端口 38401 被占用
- API Key 无效

**解决方案**：
- 确认扩展已安装
- 更改 PORT 环境变量
- 检查 API Key 是否正确

### Q3：Agent 执行任务失败？

**可能原因**：
- DOM 结构变化
- 网络延迟
- LLM 理解错误

**解决方案**：
- 检查页面是否完全加载
- 增加重试机制
- 优化任务描述

---

## 📝 测试完成后

### 生成测试报告

将测试数据整理为：
- `docs/page-agent-test-report.md`

### 决策建议

根据测试结果，建议以下三种方案之一：

**方案 1：推荐使用 Page-Agent**
- 条件：成功率 >= 90%，延迟 < 2秒
- 行动：集成到项目

**方案 2：混合使用**
- 条件：成功率 >= 70%，部分场景不稳定
- 行动：简单任务用 Page-Agent，复杂任务用 Browser Use

**方案 3：暂不使用**
- 条件：成功率 < 70% 或严重问题
- 行动：使用 Browser Use，3个月后重新评估

---

**准备好了吗？开始测试吧！** 🚀
