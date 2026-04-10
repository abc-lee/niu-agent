# Page-Agent 技术测试计划

> 测试时间：2026-04-10
> 测试目标：验证 Page-Agent 的可用性、性能、稳定性，决定是否采用

---

## 一、测试背景

### 技术概况

| 项目 | 信息 |
|------|------|
| **项目名称** | Page-Agent |
| **开源方** | 阿里巴巴 |
| **GitHub** | https://github.com/alibaba/page-agent |
| **Stars** | 9,000+ |
| **License** | MIT |
| **版本** | 1.7.1 |
| **架构** | 纯前端 JavaScript |
| **依赖** | 无后端、无Python、无浏览器插件（基础版） |

### 核心特性

- ✅ **纯 JS 实现**：无需后端、Python、无头浏览器
- ✅ **一行代码集成**：`<script src="..."></script>`
- ✅ **基于 DOM 解析**：无需截图，Token 高效
- ✅ **支持通义千问**：集成阿里云 DashScope API
- ✅ **MCP Server**：支持 MCP 协议（Beta，需 Chrome 扩展）
- ✅ **跨页面任务**：可选 Chrome 扩展支持

---

## 二、测试方案

### 方案 A：MCP Server 集成测试（推荐）

**目标**：验证 MCP Server 功能，评估是否能直接集成到现有项目

**架构**：
```
ai-bot Agent (MCP Client)
    ↓ stdio
@page-agent/mcp (Node.js)
    ↓ WebSocket (localhost:38401)
Page Agent Extension (Chrome)
    ↓ MultiPage Agent
浏览器自动化
```

**前置条件**：
1. ✅ Node.js >= 20（本项目已满足）
2. ⚠️ 安装 Chrome 扩展：[Page Agent Extension](https://chromewebstore.google.com/detail/page-agent-ext/akldabonmimlicnjlflnapfeklbfemhj)
3. ⚠️ 通义千问 API Key（需申请）

**测试步骤**：

#### 1. 安装 Chrome 扩展

**操作**：
1. 打开 Chrome 扩展商店链接
2. 安装 Page Agent Extension
3. 验证扩展图标出现在工具栏

**预期结果**：
- 扩展成功安装
- 扩展 ID：`akldabonmimlicnjlflnapfeklbfemhj`

#### 2. 配置 MCP Server

**操作**：
```bash
# 在 ai-bot 项目中安装 MCP Server
npm install -g @page-agent/mcp

# 或者使用 npx（推荐）
npx -y @page-agent/mcp
```

**配置文件**（`config/mcp-servers.yaml`）：
```yaml
page-agent-mcp:
  command: npx
  args:
    - "-y"
    - "@page-agent/mcp"
  env:
    LLM_BASE_URL: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_API_KEY: "sk-xxx"  # 需要申请通义千问 API Key
    LLM_MODEL_NAME: "qwen3.5-plus"
    PORT: "38401"
```

**预期结果**：
- MCP Server 成功启动
- 自动打开浏览器页面（launcher page）
- 扩展打开 Hub tab 并连接

#### 3. 测试 MCP 工具

**测试 3.1：get_status**

**操作**：
```python
# 在 Agent 中调用
from agent.tool_registry import get_registry

registry = get_registry()
status = registry.get("page-agent-mcp/get_status")()
print(status)
```

**预期结果**：
```json
{
  "connected": true,
  "busy": false
}
```

**测试 3.2：execute_task（简单任务）**

**任务**：打开百度，搜索"Python教程"

**操作**：
```python
result = registry.get("page-agent-mcp/execute_task")(
    task="打开百度，搜索'Python教程'"
)
print(result)
```

**预期结果**：
- 浏览器自动打开百度
- 自动输入搜索关键词
- 自动点击搜索按钮
- 返回执行结果

**测试 3.3：execute_task（复杂任务）**

**任务**：打开淘宝，搜索"Python书籍"，找出价格最低的三本书

**操作**：
```python
result = registry.get("page-agent-mcp/execute_task")(
    task="打开淘宝，搜索'Python书籍'，找出价格最低的三本书"
)
print(result)
```

**预期结果**：
- 自动打开淘宝
- 自动搜索
- 自动提取价格信息
- 返回排序后的结果

**测试 3.4：stop_task**

**操作**：
```python
# 先启动一个长时间任务
# 然后中途停止
result = registry.get("page-agent-mcp/stop_task")()
print(result)
```

**预期结果**：
- 任务成功停止
- 返回停止确认

#### 4. 性能测试

**测试 4.1：延迟测试**

**方法**：测量从调用 `execute_task` 到返回结果的时间

**测试任务**：
1. 简单任务：点击按钮
2. 中等任务：填写表单
3. 复杂任务：跨页面数据采集

**记录指标**：
- 任务执行时间
- LLM 响应时间
- DOM 操作时间

**预期结果**：
- 简单任务：< 2秒
- 中等任务：< 5秒
- 复杂任务：< 10秒

**测试 4.2：Token 消耗测试**

**方法**：统计 LLM API 调用的 Token 消耗

**对比**：
- Page-Agent（DOM 解析）
- Browser Use（截图识别）

**预期结果**：
- Page-Agent Token 消耗 < Browser Use 的 1/10

#### 5. 稳定性测试

**测试 5.1：连续执行**

**方法**：连续执行 10 次任务，观察稳定性

**预期结果**：
- 成功率 >= 90%
- 无崩溃或卡死

**测试 5.2：异常处理**

**方法**：
1. 测试无效任务描述
2. 测试网络异常情况
3. 测试页面加载失败

**预期结果**：
- 返回清晰的错误信息
- 不会导致崩溃

---

### 方案 B：纯前端集成测试（备选）

**目标**：测试基础功能，不依赖 Chrome 扩展

**架构**：
```
测试页面（HTML）
    ↓ CDN 引入
Page Agent (纯前端)
    ↓ 直接操作 DOM
浏览器自动化（单页面）
```

**前置条件**：
1. ✅ 无需额外安装
2. ✅ 使用免费的 Demo API（技术评估用）

**测试步骤**：

#### 1. 创建测试页面

**文件**：`test-page-agent.html`

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>Page-Agent 测试页面</title>
</head>
<body>
    <h1>Page-Agent 功能测试</h1>

    <!-- 测试表单 -->
    <form id="test-form">
        <input type="text" id="name" placeholder="姓名">
        <input type="email" id="email" placeholder="邮箱">
        <button type="submit">提交</button>
    </form>

    <!-- 测试按钮 -->
    <button id="test-btn">点击测试</button>

    <!-- 引入 Page-Agent Demo -->
    <script src="https://registry.npmmirror.com/page-agent/1.7.1/files/dist/iife/page-agent.demo.js"
            crossorigin="true"></script>

    <script>
        // 初始化 Page-Agent
        const agent = new PageAgent.PageAgent({
            language: 'zh-CN'
        });

        // 测试 1：点击按钮
        async function testClick() {
            console.log('测试：点击按钮');
            await agent.execute('点击"点击测试"按钮');
        }

        // 测试 2：填写表单
        async function testForm() {
            console.log('测试：填写表单');
            await agent.execute('填写姓名为"张三"，邮箱为"test@example.com"');
        }

        // 测试 3：提取信息
        async function testExtract() {
            console.log('测试：提取信息');
            await agent.execute('告诉我这个页面有哪些输入框');
        }

        // 运行所有测试
        async function runAllTests() {
            await testClick();
            await testForm();
            await testExtract();
        }

        // 绑定到按钮
        document.getElementById('test-btn').addEventListener('click', runAllTests);
    </script>
</body>
</html>
```

#### 2. 测试基础功能

**测试 2.1：点击按钮**

**操作**：打开测试页面，执行 `testClick()`

**预期结果**：
- Agent 识别按钮
- 自动点击
- 控制台输出执行过程

**测试 2.2：填写表单**

**操作**：执行 `testForm()`

**预期结果**：
- Agent 识别输入框
- 自动填写姓名和邮箱
- 输入框内容正确

**测试 2.3：提取信息**

**操作**：执行 `testExtract()`

**预期结果**：
- Agent 解析 DOM
- 返回输入框列表
- 信息准确

#### 3. 性能测试

**方法**：使用 Chrome DevTools 测量执行时间

**指标**：
- 执行延迟
- 内存占用
- CPU 使用率

---

## 三、对比测试

### Page-Agent vs Browser Use

| 维度 | Page-Agent | Browser Use |
|------|-----------|-------------|
| **后端依赖** | ❌ 无（纯前端） | ✅ 需要（Playwright） |
| **浏览器扩展** | ⚠️ 可选（跨页面需要） | ❌ 不需要 |
| **延迟** | < 100ms（DOM 直接） | ~1s（进程通信） |
| **Token 消耗** | 低（DOM 解析） | 高（截图识别） |
| **跨域能力** | ⚠️ 受限（单页面） | ✅ 无限制 |
| **系统级操作** | ❌ 不支持 | ✅ 支持（文件上传等） |
| **MCP 支持** | ✅ 原生支持 | ⚠️ 需自己实现 |
| **成熟度** | Beta（2026年3月开源） | 成熟（2025年） |
| **文档质量** | 中（中文+英文） | 高（英文） |

### 适用场景分析

**Page-Agent 最佳场景**：
- ✅ 单页面自动化（90%场景）
- ✅ SaaS AI Copilot
- ✅ 智能表单填写
- ✅ 无障碍增强
- ✅ MCP 集成项目

**Browser Use 最佳场景**：
- ✅ 跨页面复杂任务
- ✅ 需要系统级交互（文件上传等）
- ✅ 网页爬虫（跨域）
- ✅ 高稳定性要求的场景

---

## 四、风险评估

### 技术风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| Chrome 扩展兼容性问题 | 中 | 高 | 测试主流浏览器版本 |
| 通义千问 API 限流 | 中 | 中 | 添加速率限制，备用其他模型 |
| MCP Server 不稳定（Beta） | 高 | 高 | 保留 Browser Use 作为备选 |
| 跨域限制 | 高 | 中 | 复杂任务使用 Browser Use |
| DOM 结构变化导致识别失败 | 中 | 中 | 添加重试机制 |

### 项目风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|----------|
| 学习曲线陡峭 | 低 | 低 | 文档完善，示例丰富 |
| 维护成本高 | 低 | 中 | 阿里大厂维护，更新及时 |
| 社区支持不足 | 中 | 中 | GitHub Issues 活跃 |

---

## 五、测试环境准备

### 硬件环境

- 操作系统：Windows 11 Pro
- 浏览器：Chrome 最新版
- Node.js：>= 20
- 内存：>= 8GB

### 软件依赖

**方案 A（MCP Server）**：
```bash
# 安装 Chrome 扩展
# 手动访问：https://chromewebstore.google.com/detail/page-agent-ext/akldabonmimlicnjlflnapfeklbfemhj

# 安装 MCP Server
npm install -g @page-agent/mcp

# 或者使用 npx
npx -y @page-agent/mcp
```

**方案 B（纯前端）**：
```bash
# 无需安装，直接使用 CDN
```

### API Key 申请

**通义千问**：
1. 访问：https://dashscope.aliyuncs.com/
2. 注册阿里云账号
3. 开通 DashScope 服务
4. 获取 API Key

**免费额度**：
- 新用户：100万 Tokens 免费额度
- 有效期：3个月

---

## 六、测试执行计划

### 时间安排

| 阶段 | 时间 | 任务 |
|------|------|------|
| **环境准备** | 1小时 | 安装扩展、申请 API Key、配置 MCP |
| **方案 A 测试** | 2小时 | MCP Server 集成测试 |
| **方案 B 测试** | 1小时 | 纯前端测试 |
| **对比分析** | 1小时 | 整理数据，编写报告 |
| **总计** | 5小时 | 完整测试流程 |

### 测试人员

- 主测试：开发者本人
- 协助：Claude Code（自动化测试脚本编写）

---

## 七、测试报告模板

### 1. 环境信息

```
- 操作系统：Windows 11 Pro
- Chrome 版本：xxx
- Node.js 版本：xxx
- Page-Agent 版本：1.7.1
- 测试日期：2026-04-10
```

### 2. 测试结果总览

| 测试项 | 方案 A（MCP） | 方案 B（前端） |
|--------|-------------|--------------|
| 环境搭建 | ✅/⚠️/❌ | ✅/⚠️/❌ |
| 基础功能 | ✅/⚠️/❌ | ✅/⚠️/❌ |
| 性能表现 | ✅/⚠️/❌ | ✅/⚠️/❌ |
| 稳定性 | ✅/⚠️/❌ | ✅/⚠️/❌ |

### 3. 详细测试数据

#### 3.1 功能测试

| 测试用例 | 结果 | 耗时 | 备注 |
|---------|------|------|------|
| 点击按钮 | ✅/❌ | xxx ms | |
| 填写表单 | ✅/❌ | xxx ms | |
| 提取信息 | ✅/❌ | xxx ms | |
| 跨页面任务 | ✅/❌ | xxx ms | |

#### 3.2 性能测试

| 测试项 | 数据 | 对比（Browser Use） |
|--------|------|-------------------|
| 平均延迟 | xxx ms | xxx ms |
| Token 消耗 | xxx | xxx |
| 内存占用 | xxx MB | xxx MB |
| CPU 使用率 | xx% | xx% |

#### 3.3 稳定性测试

| 测试项 | 成功率 | 失败原因 |
|--------|--------|---------|
| 连续执行 10 次 | xx% | |
| 异常处理 | ✅/❌ | |

### 4. 发现的问题

| 问题 | 严重程度 | 描述 | 解决方案 |
|------|---------|------|----------|
| xxx | 高/中/低 | | |
| xxx | 高/中/低 | | |

### 5. 对比结论

**Page-Agent 优势**：
1. xxx
2. xxx

**Page-Agent 劣势**：
1. xxx
2. xxx

**Browser Use 优势**：
1. xxx
2. xxx

**Browser Use 劣势**：
1. xxx
2. xxx

### 6. 最终建议

**推荐方案**：方案 A / 方案 B / 混合架构 / 不使用

**理由**：
1. xxx
2. xxx

**后续行动计划**：
- [ ] xxx
- [ ] xxx

---

## 八、快速测试脚本

### 方案 A 快速测试

```bash
#!/bin/bash
# test-page-agent-mcp.sh

echo "=== Page-Agent MCP Server 快速测试 ==="

# 1. 检查 Node.js 版本
echo "[1/5] 检查 Node.js 版本..."
node --version

# 2. 启动 MCP Server（前台运行）
echo "[2/5] 启动 MCP Server..."
npx -y @page-agent/mcp &

# 3. 等待启动
echo "[3/5] 等待 MCP Server 启动..."
sleep 5

# 4. 检查状态
echo "[4/5] 检查连接状态..."
# 需要实际调用 get_status 工具

# 5. 测试任务
echo "[5/5] 测试简单任务..."
# 需要实际调用 execute_task 工具

echo "=== 测试完成 ==="
```

### 方案 B 快速测试

```bash
#!/bin/bash
# test-page-agent-frontend.sh

echo "=== Page-Agent 纯前端快速测试 ==="

# 1. 创建测试页面
echo "[1/3] 创建测试页面..."
cat > /tmp/test-page-agent.html << 'EOF'
<!-- 测试页面内容 -->
EOF

# 2. 打开浏览器
echo "[2/3] 打开测试页面..."
start /tmp/test-page-agent.html

# 3. 手动测试
echo "[3/3] 请手动执行测试..."

echo "=== 测试完成 ==="
```

---

## 九、预期结论

### 乐观情况（推荐使用）

**如果测试通过**：
- ✅ MCP Server 稳定可用
- ✅ 性能符合预期（延迟 < 2秒）
- ✅ Token 消耗低
- ✅ 成功率 >= 90%

**推荐方案**：
- 主要使用 Page-Agent（单页面任务）
- 保留 Browser Use 作为备选（复杂任务）

### 中等情况（部分使用）

**如果部分功能不稳定**：
- ⚠️ MCP Server 偶尔失败
- ⚠️ 某些场景性能不佳

**推荐方案**：
- 纯前端方案（方案 B）用于简单场景
- Browser Use 用于复杂场景
- MCP Server 等成熟后再集成

### 悲观情况（暂不使用）

**如果测试失败**：
- ❌ MCP Server 不可用
- ❌ 性能不达标
- ❌ 稳定性差

**推荐方案**：
- 使用 Browser Use
- 3个月后重新评估 Page-Agent

---

**测试计划完成时间**：2026-04-10
**预计测试完成时间**：2026-04-11
**报告提交时间**：2026-04-11
