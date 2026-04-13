# 浏览器自动化 Skill

**触发关键词**：浏览器、网页、填表、截图、网页操作、表单填写、自动答题、上网查

**L1 摘要**：Browser automation|browser,form filling,web operation|Use browser_navigate + browser_interact to automate browser tasks|browser_navigate,browser_interact,Chrome Extension|skill|memory/skills/browser-automation.md

## 概述

本项目提供浏览器自动化能力，基于 Chrome Extension 实现。采用 **MCP 工具 + Chrome Extension** 架构：

1. **`browser_navigate` MCP 工具**：启动浏览器并导航到 URL，自动返回页面结构化状态
2. **`browser_interact` MCP 工具**：通过元素编号操作页面（点击、输入、选择、滚动）

## 架构说明

```
browser_navigate("https://example.com")
    ↓ 启动浏览器 + Chrome Extension
    ↓ Extension 在页面内遍历 DOM，给交互元素编号
    ↓ 返回结构化状态给 LLM
LLM 看到：
    [0]<a aria-label=首页 />
    [1]<input type=text name=username placeholder=请输入用户名 />
    [2]<input type=password name=password />
    [3]<button type=submit>登录 />
LLM 决策 → browser_interact(action="click", index=3)
    ↓ 通过 WebSocket 告诉 Extension
    ↓ Extension 执行点击 + 返回新页面状态
LLM 看到新状态 → 继续决策 or 向用户汇报
```

## 工作循环（必须遵守）

每次浏览器操作后，**必须根据返回的页面状态决定下一步**。

```
1. browser_navigate("url")          → 导航到目标页面，获得编号的交互元素
2. 根据元素编号决策下一步           → 点击哪个按钮？填哪个输入框？
3. browser_interact(action, index)  → 执行操作，获得新的页面状态
4. 根据新状态继续决策              → 继续 or 向用户汇报
```

**核心原则**：
- `browser_navigate` 返回的 `elements` 包含所有可交互元素的编号
- 用 `browser_interact(action="click", index=N)` 操作编号为 N 的元素
- 每次操作后自动返回新的页面状态，无需额外获取
- 如果页面状态不完整，用 `browser_interact(action="get_state")` 刷新

## 使用流程

### 1. 导航到网页

**调用 `browser_navigate` 工具**：

```json
{
  "url": "https://example.com"
}
```

**返回值**（自动包含页面状态）：

```json
{
  "status": "success",
  "url": "https://example.com/login",
  "title": "用户登录",
  "elements": "[0]<input type=text name=username placeholder=请输入用户名 />\n[1]<input type=password name=password />\n[2]<button type=submit>登录 />",
  "pageInfo": { "viewportWidth": 1280, "viewportHeight": 720, "pixelsBelow": 0 }
}
```

### 2. 操作页面元素

根据返回的 `elements` 中的编号，使用 `browser_interact` 操作：

**点击按钮**：
```json
{ "action": "click", "index": 2 }
```

**输入文本**：
```json
{ "action": "input", "index": 0, "text": "张三" }
```

**选择下拉选项**：
```json
{ "action": "select", "index": 5, "option": "中国" }
```

**滚动页面**：
```json
{ "action": "scroll", "direction": "down", "amount": 1.0 }
```

**刷新页面状态**：
```json
{ "action": "get_state" }
```

## 完整示例

### 场景：自动填写登录表单

**步骤 1**：导航到登录页面

```json
{ "url": "https://example.com/login" }
```

返回：
```
[0]<input type=text name=username placeholder=请输入用户名 />
[1]<input type=password name=password />
[2]<button type=submit>登录 />
```

**步骤 2**：填写用户名

```json
{ "action": "input", "index": 0, "text": "zhangsan" }
```

**步骤 3**：填写密码

```json
{ "action": "input", "index": 1, "text": "mypassword" }
```

**步骤 4**：点击登录

```json
{ "action": "click", "index": 2 }
```

返回新的页面状态，LLM 根据新状态决定下一步。

### 场景：上网查新闻

**步骤 1**：导航到新闻网站

```json
{ "url": "https://news.baidu.com" }
```

**步骤 2**：根据返回的元素编号，点击感兴趣的新闻链接

```json
{ "action": "click", "index": 5 }
```

**步骤 3**：阅读新闻内容，向用户汇报

## 注意事项

### 1. 浏览器生命周期

- **自动启动**：首次调用 `browser_navigate` 时自动启动浏览器
- **浏览器可见**：始终显示浏览器窗口
- **Extension 常驻**：Chrome Extension 在每个页面自动运行
- **持久化登录**：用户数据保存在 `~/.niu/browser_ext_profile/`
- **新标签页**：Extension 自动注入，无需额外处理

### 2. 元素编号规则

- 编号从 0 开始，按 DOM 顺序递增
- 只有**可交互元素**才有编号（按钮、链接、输入框、下拉框等）
- 新出现的元素用 `*[index]` 标记（带星号）
- 页面变化后编号可能改变，**每次操作后都要看新的编号**

### 3. 限制

- **无反爬虫绕过**：CAPTCHA、Cloudflare 可能阻止访问
- **纯图片页面**：Canvas/WebGL 页面无 DOM 控件，elements 为空
- **iframe 内容**：跨域 iframe 内的元素可能无法检测

## 关键词

浏览器、网页、填表、截图、自动化、表单填写、自动答题、Chrome Extension
